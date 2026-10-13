# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``_augment_glue_connection_config`` (p31 task 4.1).

``_augment_glue_connection_config`` (``handlers/pipelines_jdbc.py:247``) is on
the critical path for ``glue_connection`` mode on **both** sides of a JDBC
pipeline: ``create_pipeline`` calls it for ``source_config``
(``handlers/pipelines_crud.py:548``) and again for ``target_config``
(``:606``), translating any ``ValueError`` it raises into an HTTP 400.

Test groups:
    1. ``TestUnresolvableConnection`` — every path that raises: connection
       does not exist, the Glue call fails, and the engine cannot be
       derived from the connection's JDBC URL. Includes the two
       ``create_pipeline`` call sites, so the ``ValueError`` → 400
       translation is asserted rather than assumed.
    2. ``TestEngineResolution`` — the engine is recovered from the
       connection's ``JDBC_CONNECTION_URL`` prefix for every prefix the
       resolver recognises, on both sides, and falls back to the config's
       own ``jdbc_url`` when the connection carries no URL property.
    3. ``TestSecretArnResolution`` — ``SECRET_ID`` is the only source of the
       secret (the ``adi:secret_arn`` tag is unreadable), the resolved value is
       always a Secrets Manager **ARN reference** (invariant
       ``LGR-819980f-1``), and a connection with no secret at all is fatal on
       **both** sides. That last case used to be covered as a per-side
       asymmetry — fatal for a source, tolerated for a target, because the
       target's runtime path copied the connection's inline credentials into
       job arguments. That copy was the invariant's violation and is gone, so
       the tolerance is withdrawn (superseding part of ``LGR-c246ab8-1``) and
       the test now asserts symmetry, including the target-side HTTP 400.
    4. ``TestNoOpPaths`` — the guards that must not call Glue at all,
       including the ``_connection_mode`` early return at
       ``pipelines_jdbc.py:288-289`` that is why a ``select_rds`` target
       receives no augmentation whatsoever.

Complements ``test_jdbc_pipeline_lifecycle.py::TestAugmentGlueConnectionConfig``,
which covers the happy-path tag/property resolution for postgres and mysql.
Nothing here duplicates those cases.

Validates: Requirements 9.4
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Stub the Lambda-layer modules that ``handlers.pipelines_crud`` imports at
# module scope, so the handler imports cleanly in a unit-test process without
# the real layer. Same bootstrap as ``test_pipelines_manual_mode.py`` and
# ``test_jdbc_pipeline_lifecycle.py``; a no-op when one of those files has
# already installed it. ``sys.path`` itself is set up by this directory's
# ``conftest.py``.
# ---------------------------------------------------------------------------

sys.modules.setdefault("eventbridge_utils", MagicMock())
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

if "audit_utils" not in sys.modules or not isinstance(sys.modules["audit_utils"], MagicMock):
    _mock_audit_utils = MagicMock()
    _mock_audit_utils.extract_request_context = MagicMock(return_value={})
    _mock_audit_utils.write_audit_entry = MagicMock()
    sys.modules["audit_utils"] = _mock_audit_utils

_prev_env_utils = sys.modules.get("env_utils")
if "env_utils" not in sys.modules or not isinstance(sys.modules["env_utils"], MagicMock):
    _mock_env_utils = MagicMock()
    _mock_env_utils.require_env = lambda name, default=None: {
        "UPLOAD_BUCKET": "agentic-data-ingestion-source",
        "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
        "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    }.get(name, default or name)
    sys.modules["env_utils"] = _mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

try:
    from handlers.pipelines_crud import create_pipeline
    from handlers.pipelines_jdbc import SECRET_ARN_RE, _augment_glue_connection_config

    from jdbc_helpers import SUPPORTED_ENGINES
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

CONNECTION_NAME = "customer-owned-postgres"

# A full, well-formed Secrets Manager ARN. Deliberately shaped to satisfy
# ``SECRET_ARN_RE`` so the C8 assertion below is meaningful: what the
# augmenter puts on the config must be a *reference*, never a credential.
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adi/db-creds-AbCdEf"
LEGACY_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:legacy/db-creds-ZyXwVu"

# A Glue Connection carrying credentials INLINE rather than by secret
# reference. These are placeholders for a shape the augmenter must handle,
# never values it may propagate — the assertions on them are all negative
# (invariant ``LGR-819980f-1``).
_INLINE_CRED_PROPS: dict[str, str] = {
    "JDBC_CONNECTION_URL": "jdbc:postgresql://customer-db.internal:5432/orders",
    "USERNAME": "placeholder-user-not-a-credential",
    "PASSWORD": "placeholder-value-not-a-credential",
}

# JDBC URL prefix → the canonical engine the resolver recovers from it.
# Aurora variants share a prefix with their non-Aurora counterpart, so the
# resolver deliberately returns the non-Aurora value.
_PREFIX_TO_ENGINE: list[tuple[str, str]] = [
    ("jdbc:postgresql://host:5432/db", "postgres"),
    ("jdbc:mysql://host:3306/db", "mysql"),
    ("jdbc:mariadb://host:3306/db", "mariadb"),
    ("jdbc:oracle:thin:@host:1521:db", "oracle-se2"),
    ("jdbc:sqlserver://host:1433;databaseName=db", "sqlserver-se"),
]

_FIELD_PREFIXES = ("source_config", "target_config")


class _FakeEntityNotFound(Exception):
    """Stand-in for ``glue.exceptions.EntityNotFoundException``.

    The handler catches the exception off the client instance, so the mock
    must expose a real exception *class* — a bare ``MagicMock`` attribute
    makes the ``except`` clause raise ``TypeError``.
    """


class _FakeInvalidInput(Exception):
    """Stand-in for Glue's ``InvalidInputException``.

    Raised by the mock's ``get_tags`` so the fake behaves like the real API,
    which refuses to tag or read tags on a Glue Connection.
    """


def _glue_client(
    *,
    props: dict[str, str] | None = None,
    tags: dict[str, str] | None = None,
    error: BaseException | None = None,
) -> MagicMock:
    """Return a mock Glue client for the augmenter to read.

    ``props`` becomes the connection's ``ConnectionProperties``. ``tags`` records
    that a tag notionally exists but is **never readable** — see the ``get_tags``
    wiring below. ``error`` makes ``get_connection`` raise.

    ``get_caller_identity`` is stubbed because the same mock is returned
    for the ``sts`` client that ``_get_glue_connection_tags_safe`` builds
    while constructing the connection ARN.
    """
    glue = MagicMock()
    glue.exceptions.EntityNotFoundException = _FakeEntityNotFound
    if error is not None:
        glue.get_connection.side_effect = error
    else:
        glue.get_connection.return_value = {
            "Connection": {
                "Name": CONNECTION_NAME,
                "ConnectionProperties": props or {},
            }
        }
    # `get_tags` RAISES, because that is what real Glue does for a connection
    # ARN. Glue Connections are not a taggable resource type: both `GetTags` and
    # `TagResource` reject `arn:aws:glue:…:connection/<name>` with
    # `InvalidInputException`, verified against the live API — including on
    # connections created by our own provisioner while it passed
    # `Tags={"adi:secret_arn": …}` to `CreateConnection`. Tagging a Glue
    # *database* succeeds, which rules out a credentials or ARN-format problem.
    #
    # This mock used to return `{"Tags": tags}`, which made the suite assert a
    # credential read path that cannot exist in production. That fiction is why a
    # connection carrying no `SECRET_ID` passed every test here and then failed
    # pipeline creation with an error whose suggested remediation — "add the
    # adi:secret_arn tag" — the AWS API refuses to perform.
    #
    # `tags` is still accepted so a caller can state that a tag is present and
    # still unreadable; it deliberately does NOT become a readable value.
    glue.get_tags.side_effect = _FakeInvalidInput("Glue Connections are not taggable; GetTags rejects a connection ARN")
    glue.get_caller_identity.return_value = {"Account": "123456789012"}
    return glue


def _glue_connection_config(**overrides) -> dict:
    """A minimal ``glue_connection``-mode config the augmenter will act on."""
    config: dict = {
        "_connection_mode": "glue_connection",
        "glue_connection_name": CONNECTION_NAME,
    }
    config.update(overrides)
    return config


def _client_error(code: str = "AccessDeniedException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "not permitted"}}, "GetConnection")


# ---------------------------------------------------------------------------
# 1. Unresolvable connection — the ValueError (→ 400) paths
# ---------------------------------------------------------------------------


class TestUnresolvableConnection:
    """Every branch that raises, plus the caller's 400 translation.

    R9.4: "raises ``ValueError`` (→ 400) on an unresolvable connection".
    """

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_nonexistent_connection_raises_value_error(self, field_prefix: str):
        """A connection Glue does not know about is fatal on BOTH sides.

        The per-side asymmetry is about a *missing secret*, not about a
        missing connection — neither side can proceed without one.
        """
        config = _glue_connection_config()
        glue = _glue_client(error=_FakeEntityNotFound("no such connection"))

        with (
            patch("handlers.pipelines_jdbc.boto3.client", return_value=glue),
            pytest.raises(ValueError) as excinfo,
        ):
            _augment_glue_connection_config(config, field_prefix)

        message = str(excinfo.value)
        assert f"{field_prefix}.glue_connection_name" in message
        assert CONNECTION_NAME in message
        assert "does not exist" in message
        # Nothing was resolved onto the config.
        assert "secret_arn" not in config
        assert "engine" not in config

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_glue_client_error_is_wrapped_as_value_error(self, field_prefix: str):
        """A Glue API failure surfaces as a 400-able ValueError, not a 500."""
        config = _glue_connection_config()
        glue = _glue_client(error=_client_error())

        with (
            patch("handlers.pipelines_jdbc.boto3.client", return_value=glue),
            pytest.raises(ValueError, match="Failed to fetch Glue Connection"),
        ):
            _augment_glue_connection_config(config, field_prefix)

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_underivable_engine_raises_and_names_accepted_prefixes(self, field_prefix: str):
        """An unrecognised JDBC URL prefix cannot yield an engine.

        Covers ``pipelines_jdbc.py:366-373``. The remediation text must
        name the prefixes that would work, so the operator can fix the
        connection rather than guess.
        """
        config = _glue_connection_config()
        glue = _glue_client(
            # SECRET_ID, not a tag: the credential only has to be *present* so
            # this test can reach the engine check, and a tag is unreadable.
            props={
                "JDBC_CONNECTION_URL": "jdbc:db2://legacy-host:50000/warehouse",
                "SECRET_ID": SECRET_ARN,
            },
        )

        with (
            patch("handlers.pipelines_jdbc.boto3.client", return_value=glue),
            pytest.raises(ValueError) as excinfo,
        ):
            _augment_glue_connection_config(config, field_prefix)

        message = str(excinfo.value)
        assert f"{field_prefix}.engine could not be derived" in message
        assert "jdbc:db2://legacy-host:50000/warehouse" in message
        for prefix in ("jdbc:postgresql:", "jdbc:mysql:", "jdbc:mariadb:", "jdbc:oracle:", "jdbc:sqlserver:"):
            assert prefix in message
        assert "engine" not in config

    def test_source_side_unresolvable_connection_returns_http_400(self):
        """``create_pipeline`` turns the source-side ValueError into a 400.

        Asserted against the real call site (``pipelines_crud.py:548-551``)
        rather than assumed from the raise.
        """
        body = json.dumps(
            {
                "pipeline_name": "jdbc glue-connection source",
                "target_schema": "uploaded_test_schema",
                "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
                "source_type": "JDBC",
                "source_config": _glue_connection_config(),
            }
        )
        glue = _glue_client(error=_FakeEntityNotFound("no such connection"))

        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            result = create_pipeline(body)

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "source_config.glue_connection_name" in error
        assert CONNECTION_NAME in error

    def test_target_side_unresolvable_connection_returns_http_400(self):
        """``create_pipeline`` turns the target-side ValueError into a 400.

        The target call site (``pipelines_crud.py:604-610``) fires for every
        ``JDBC`` / ``RDS_DATABASE`` target regardless of connection mode, so
        this is the one create-time check a ``glue_connection`` target gets.
        """
        body = json.dumps(
            {
                "pipeline_name": "jdbc glue-connection target",
                "target_schema": "uploaded_test_schema",
                "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
                "target_config": _glue_connection_config(
                    type="JDBC",
                    jdbc_url="jdbc:postgresql://customer-db.internal:5432/orders",
                    jdbc_driver="org.postgresql.Driver",
                    jdbc_table="public.orders",
                ),
            }
        )
        glue = _glue_client(error=_FakeEntityNotFound("no such connection"))

        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            result = create_pipeline(body)

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "target_config.glue_connection_name" in error
        assert CONNECTION_NAME in error


# ---------------------------------------------------------------------------
# 2. Engine resolution
# ---------------------------------------------------------------------------


class TestEngineResolution:
    """``engine`` is recovered from the connection's JDBC URL prefix."""

    @pytest.mark.parametrize(("jdbc_url", "expected_engine"), _PREFIX_TO_ENGINE)
    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_engine_derived_from_connection_url_prefix(self, field_prefix: str, jdbc_url: str, expected_engine: str):
        """Every prefix the resolver recognises yields a supported engine.

        Runs for both ``source_config`` and ``target_config`` — engine
        derivation is side-independent (R9.4: both sides).
        """
        config = _glue_connection_config()
        glue = _glue_client(
            props={"JDBC_CONNECTION_URL": jdbc_url, "SECRET_ID": SECRET_ARN},
        )

        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            _augment_glue_connection_config(config, field_prefix)

        assert config["engine"] == expected_engine
        assert config["engine"] in SUPPORTED_ENGINES

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_engine_falls_back_to_config_jdbc_url(self, field_prefix: str):
        """With no URL property on the connection, the config's own URL is used.

        Covers the ``or config.get("jdbc_url", "")`` fallback at
        ``pipelines_jdbc.py:364`` — the wizard forwards the URL it read off
        the connection even when the property is absent from the API response.
        """
        config = _glue_connection_config(jdbc_url="jdbc:mariadb://reporting-db:3306/analytics")
        glue = _glue_client(props={"SECRET_ID": SECRET_ARN})

        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            _augment_glue_connection_config(config, field_prefix)

        assert config["engine"] == "mariadb"

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_preexisting_engine_is_not_overwritten(self, field_prefix: str):
        """A caller-supplied engine survives augmentation.

        The connection's URL implies ``postgres``; the config already says
        ``aurora-postgresql``, which the URL prefix cannot express. The more
        specific value must win.
        """
        config = _glue_connection_config(engine="aurora-postgresql")
        # URL + SECRET_ID rather than the inline-credential props: Glue rejects
        # SECRET_ID alongside USERNAME/PASSWORD, so a fixture carrying both is a
        # shape the API would never return.
        glue = _glue_client(
            props={
                "JDBC_CONNECTION_URL": _INLINE_CRED_PROPS["JDBC_CONNECTION_URL"],
                "SECRET_ID": SECRET_ARN,
            }
        )

        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            _augment_glue_connection_config(config, field_prefix)

        assert config["engine"] == "aurora-postgresql"


# ---------------------------------------------------------------------------
# 3. secret_arn resolution — invariant LGR-819980f-1 / LGR-819980f-2
# ---------------------------------------------------------------------------


class TestSecretArnResolution:
    """The secret is resolved as an ARN reference, and only as a reference."""

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_secret_id_property_is_the_only_source_and_a_tag_cannot_substitute(self, field_prefix: str):
        """``SECRET_ID`` is the sole credential reference; the tag is unreadable.

        **This test previously asserted the opposite** — that an ``adi:secret_arn``
        tag takes precedence over ``SECRET_ID`` — and it passed only because the
        fixture mocked ``get_tags`` into returning a value. Real Glue rejects
        ``GetTags`` on a connection ARN outright (``InvalidInputException``),
        because Connections are not a taggable resource type, so the tag branch
        could never win and in production never ran at all.

        ``LGR-819980f-2`` remains correct on its load-bearing half: the ARN lives
        in ``SECRET_ID`` in ``ConnectionProperties``, and that is what Glue
        consults at job runtime. Its secondary claim — that the tag is a working
        "read-path mirror" — is what this test now pins as false.

        Asserted as ``LEGACY_SECRET_ARN``, the value in ``SECRET_ID``, with a
        notional tag naming a *different* ARN present: if a tag path is ever
        reintroduced this assertion fails rather than passing vacuously.
        """
        config = _glue_connection_config()
        glue = _glue_client(
            props={
                "JDBC_CONNECTION_URL": "jdbc:postgresql://host:5432/db",
                "SECRET_ID": LEGACY_SECRET_ARN,
            },
            tags={"adi:secret_arn": SECRET_ARN},
        )

        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            _augment_glue_connection_config(config, field_prefix)

        assert config["secret_arn"] == LEGACY_SECRET_ARN, (
            "secret_arn must come from the SECRET_ID connection property. Reading "
            "it from the adi:secret_arn tag is not merely deprecated, it is "
            "impossible: Glue refuses GetTags on a connection ARN."
        )

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_resolved_secret_is_an_arn_reference_never_a_credential(self, field_prefix: str):
        """INVARIANT ``LGR-819980f-1`` — credentials travel by ARN only.

        The connection carries inline ``USERNAME`` / ``PASSWORD`` alongside
        a secret reference. What lands on the config must be the ARN; the
        inline values must not appear anywhere in it.
        """
        config = _glue_connection_config()
        glue = _glue_client(
            props={**_INLINE_CRED_PROPS, "SECRET_ID": SECRET_ARN},
            tags={},
        )

        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            _augment_glue_connection_config(config, field_prefix)

        assert config["secret_arn"] == SECRET_ARN
        assert SECRET_ARN_RE.match(config["secret_arn"]), "resolved secret must be a Secrets Manager ARN reference"
        _assert_no_inline_credentials(config)

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_missing_secret_is_fatal_on_both_sides(self, field_prefix: str):
        """A connection with no SECRET_ID is refused for a source AND a target.

        **This test used to assert an asymmetry** (it was
        ``test_missing_secret_is_fatal_for_a_source_but_tolerated_for_a_target``):
        the same inline-credential connection was fatal for a source and accepted
        for a target, with ``secret_arn`` left unset "so the runtime path resolves
        the connection's own credentials". That runtime path copied the inline
        USERNAME/PASSWORD into ``--target_jdbc_user`` / ``--target_jdbc_password``
        Glue job arguments — the violation of standing invariant
        ``LGR-819980f-1`` that leaked a target credential. It is deleted:
        ``build_jdbc_arguments`` now raises when no Secrets Manager reference
        resolves, so a target accepted here would fail at Glue-job creation
        instead. Refusing at the API is the same answer, given sooner. This
        supersedes the part of ``LGR-c246ab8-1`` that relied on the tolerance.
        """
        config = _glue_connection_config()

        with (
            patch(
                "handlers.pipelines_jdbc.boto3.client",
                return_value=_glue_client(props=_INLINE_CRED_PROPS, tags={}),
            ),
            pytest.raises(ValueError) as excinfo,
        ):
            _augment_glue_connection_config(config, field_prefix)

        message = str(excinfo.value)
        assert f"{field_prefix}.secret_arn is required" in message
        assert CONNECTION_NAME in message
        # SECRET_ID, and explicitly NOT the tag. This assertion used to require
        # "adi:secret_arn" in the message, which pinned a remediation the AWS API
        # refuses to perform — Glue rejects TagResource on a connection ARN — so
        # the one instruction the operator was given could not be carried out.
        assert "SECRET_ID" in message
        assert "adi:secret_arn" not in message, (
            "The remediation must not tell an operator to add a tag: Glue "
            "Connections are not taggable, so that instruction cannot be followed."
        )
        # The mutual exclusivity has to be stated, because adding SECRET_ID while
        # USERNAME/PASSWORD remain is rejected by Glue.
        assert "USERNAME/PASSWORD" in message
        assert "select_rds" in message and "manual" in message
        # The error is returned to the client: it may name the connection, never
        # carry the connection's inline credentials.
        assert _INLINE_CRED_PROPS["USERNAME"] not in message
        assert _INLINE_CRED_PROPS["PASSWORD"] not in message

        # Nothing was resolved onto the config, and nothing inline was copied.
        assert "secret_arn" not in config
        _assert_no_inline_credentials(config)

    def test_target_side_connection_without_secret_id_returns_http_400(self):
        """``create_pipeline`` turns the target-side refusal into a 400.

        The real call site, not the function alone: before the fix this request
        returned 201 and stored a pipeline that would only fail at Glue-job
        creation, three steps into its first run.
        """
        body = json.dumps(
            {
                "pipeline_name": "jdbc glue-connection target, inline creds",
                "target_schema": "uploaded_test_schema",
                "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
                "target_config": _glue_connection_config(
                    type="JDBC",
                    jdbc_url="jdbc:postgresql://customer-db.internal:5432/orders",
                    jdbc_driver="org.postgresql.Driver",
                    jdbc_table="public.orders",
                ),
            }
        )
        glue = _glue_client(props=_INLINE_CRED_PROPS)

        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            result = create_pipeline(body)

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "target_config.secret_arn is required" in error
        assert CONNECTION_NAME in error
        assert _INLINE_CRED_PROPS["PASSWORD"] not in result["body"]
        assert _INLINE_CRED_PROPS["USERNAME"] not in result["body"]


def _assert_no_inline_credentials(config: dict) -> None:
    """Assert no inline ConnectionProperties credential reached ``config``.

    Guards invariant ``LGR-819980f-1``: plaintext user/password must never
    leave the Glue Connection via ``source_config`` / ``target_config``,
    because those are persisted and flow into Glue job arguments.
    """
    forbidden = {_INLINE_CRED_PROPS["USERNAME"], _INLINE_CRED_PROPS["PASSWORD"]}
    leaked = {key: value for key, value in config.items() if isinstance(value, str) and value in forbidden}
    assert not leaked, f"inline Glue Connection credentials leaked onto the config: {sorted(leaked)}"
    for key in ("USERNAME", "PASSWORD", "jdbc_user", "jdbc_password"):
        assert key not in config, f"{key} must never be populated from ConnectionProperties"


# ---------------------------------------------------------------------------
# 4. The guards that must not call Glue
# ---------------------------------------------------------------------------


class TestNoOpPaths:
    """Early returns. Each one is asserted by making a Glue call an error."""

    @staticmethod
    def _exploding_glue():
        return patch(
            "handlers.pipelines_jdbc.boto3.client",
            side_effect=AssertionError("Glue must not be called"),
        )

    @pytest.mark.parametrize("mode", ["select_rds", "manual", None, "", "GLUE_CONNECTION"])
    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_non_glue_connection_mode_is_a_noop(self, field_prefix: str, mode: str | None):
        """Only an exact ``glue_connection`` mode is augmented.

        Covers ``pipelines_jdbc.py:288-289``, and it is why a ``select_rds``
        target receives no augmentation at all: neither this function nor
        the manual-mode validator does anything for it. The comparison is
        exact, so a differently-cased value is also skipped.
        """
        config: dict = {"glue_connection_name": CONNECTION_NAME}
        if mode is not None:
            config["_connection_mode"] = mode

        with self._exploding_glue():
            _augment_glue_connection_config(config, field_prefix)

        assert "secret_arn" not in config
        assert "engine" not in config

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_missing_connection_name_is_a_noop(self, field_prefix: str):
        """No connection name means there is nothing to read.

        The validator downstream reports the missing field; this function
        deliberately does not raise its own error for it
        (``pipelines_jdbc.py:291-295``).
        """
        config = {"_connection_mode": "glue_connection"}

        with self._exploding_glue():
            _augment_glue_connection_config(config, field_prefix)

        assert config == {"_connection_mode": "glue_connection"}

    @pytest.mark.parametrize("config", [None, "not-a-dict", [], 42])
    def test_non_dict_config_returns_without_raising(self, config):
        """A non-dict config is left for the validator's clean error.

        ``pipelines_jdbc.py:283-286`` — returning here avoids an
        ``AttributeError`` masking the real "must be an object" message.
        """
        with self._exploding_glue():
            _augment_glue_connection_config(config, "target_config")

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_fast_path_skips_glue_when_both_fields_present(self, field_prefix: str):
        """Idempotent: an already-complete config needs no Glue call.

        ``pipelines_jdbc.py:298-299``. This is what makes a second pass
        over the same config free, and keeps wizard sessions that do
        surface both values working.
        """
        config = _glue_connection_config(secret_arn=SECRET_ARN, engine="postgres")

        with self._exploding_glue():
            _augment_glue_connection_config(config, field_prefix)

        assert config["secret_arn"] == SECRET_ARN
        assert config["engine"] == "postgres"

    @pytest.mark.parametrize("field_prefix", _FIELD_PREFIXES)
    def test_engine_only_gap_still_reads_the_connection(self, field_prefix: str):
        """The fast path needs BOTH fields; one missing still calls Glue.

        Guards against the fast-path condition drifting to ``or``, which
        would leave ``engine`` unset for a config that already had a secret.
        """
        config = _glue_connection_config(secret_arn=SECRET_ARN)
        glue = _glue_client(props={"JDBC_CONNECTION_URL": "jdbc:mysql://host:3306/db"})

        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            _augment_glue_connection_config(config, field_prefix)

        assert config["engine"] == "mysql"
        assert config["secret_arn"] == SECRET_ARN
        glue.get_connection.assert_called_once_with(Name=CONNECTION_NAME)
