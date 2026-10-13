# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract: the wizard's emitted JDBC `target_config` vs. what `create_pipeline` enforces.

Two sides, pinned against each other so a change to either fails a test rather
than a tester's deploy:

* **Emitted** — the `target_config` the JDBC wizard produces in each of the
  three connection modes, as fixture data in
  `tests/fixtures/jdbc_wizard_payloads.py` with per-key provenance.
* **Enforced** — the required-key set in `create_pipeline`'s
  `elif target_type == "JDBC":` branch, read out of the handler source by AST
  rather than restated here. Restating it would make this file agree with itself
  and with nothing else; extracting it means adding or removing a required field
  on the backend fails this test until the emitted shape is updated to match.

Neither side is transcribed. ``LGR-28b4d75-5`` (template) is the ratified
pattern here and it rejects restating a field list in the test: a contract test
written from one side's own output cannot detect that side drifting, because the
output is its own oracle. So the enforced set is read from the handler by AST,
and the emitted set is checked against the three wizard components that assign
it, parsed at test time — see `TestEmittedFixtureTracksTheWizard`. The fixture
is the *values*; the frontend remains the authority on the *keys*.

**This is not a re-run of the 400 matrix.** `tests/unit/lambdas/dashboard_api/
test_jdbc_target_mode_validation.py` already covers "drop field X in mode Y,
get a specific 400" against its own minimal payload; `test_jdbc_target_manual_
validator.py` covers the manual networking validator in isolation, and
`test_glue_connection_augmentation.py` the augmenter's raise paths. The job left
over — the one a contract layer exists for — is to prove that the shape the
frontend actually sends satisfies that enforced set, in every mode, and to make
the source/target asymmetry a written, tested statement instead of something a
reader has to reconstruct from two files.

Why it matters at all: since the P17 type consolidation the wizard only emits
`type: 'JDBC'`, and until p31 task 4.2 there was no `JDBC` branch in that
if/elif chain — so the checks the `RDS_DATABASE` branch performs were
unreachable for every payload a real user could produce. A malformed target was
accepted with HTTP 201 and surfaced later as an opaque Glue job failure.
``LGR-2474614-1`` (backfilled) records that failure class: empty
``--target_jdbc_*`` arguments, which Glue rejects with "expected one argument".
The gap was invisible precisely because nothing compared the two sides.

Validates: Requirements 9.2, 9.3 (R9 contract coverage, design §6 Contract)
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures.jdbc_wizard_payloads import (
    JDBC_CONNECTION_MODES,
    create_pipeline_request_body,
    emitted_jdbc_target_config,
)

# ---------------------------------------------------------------------------
# Import the handler the way the other dashboard-API contract tests do: stub
# the Lambda-layer modules, then put the handler package on sys.path.
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

if "env_utils" not in sys.modules or not isinstance(sys.modules["env_utils"], MagicMock):
    _mock_env_utils = MagicMock()
    _mock_env_utils.require_env = lambda name, default=None: {
        "UPLOAD_BUCKET": "adi-source-files-123456789012-us-west-2",
    }.get(name, default or name)
    sys.modules["env_utils"] = _mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
# `shared/` is the Lambda layer, mounted at /opt/python in production;
# pipelines_crud imports `config` and `jdbc_helpers` from it.
for _path in (
    _REPO_ROOT / "shared",
    _REPO_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from handlers import pipelines_crud, pipelines_jdbc  # noqa: E402
from handlers.pipelines_crud import create_pipeline  # noqa: E402
from handlers.pipelines_jdbc import (  # noqa: E402
    _JDBC_MANUAL_REQUIRED_FIELDS,
    _JDBC_SOURCE_REQUIRED_FIELDS,
)

# ---------------------------------------------------------------------------
# Side 2 of the contract — read the enforced key set out of the handler source.
# ---------------------------------------------------------------------------


def _target_type_branch_required_keys(target_type: str) -> tuple[str, ...]:
    """Extract the keys `create_pipeline` requires for `target_type`.

    Walks `create_pipeline`'s AST for the branch whose test is
    ``target_type == "<target_type>"`` and collects the key of every
    ``if not target_config.get("<key>"):`` guard inside it, in source order —
    which is the order the handler reports them, so a payload missing two keys
    is rejected naming the earlier one.

    Reading the handler rather than hardcoding the answer is the whole point:
    this is what makes the test fail when the backend's requirements change.
    """
    tree = ast.parse(pathlib.Path(pipelines_crud.__file__).read_text(encoding="utf-8"))
    create_fn = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "create_pipeline"
    )

    for node in ast.walk(create_fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "target_type"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == target_type
        ):
            return tuple(_required_keys_in_branch(node.body))

    raise AssertionError(
        f"No `target_type == {target_type!r}` branch found in create_pipeline. "
        "If the target-type dispatch was restructured, this extractor needs "
        "updating — do not delete the assertion."
    )


def _required_keys_in_branch(body: list[ast.stmt]) -> list[str]:
    """Collect `k` for each `if not target_config.get("k"):` statement in `body`."""
    keys: list[str] = []
    for stmt in body:
        if not isinstance(stmt, ast.If):
            continue
        test = stmt.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            continue
        call = test.operand
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "get"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "target_config"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            keys.append(call.args[0].value)
    return keys


# The enforced set, as read from the handler at import time.
ENFORCED_JDBC_TARGET_KEYS = _target_type_branch_required_keys("JDBC")

# What we assert that set is. The pin exists so a *silent* change to the
# backend's requirements is a test failure with a readable diff, and so the
# deliberate exclusions below have somewhere to be written down.
EXPECTED_JDBC_TARGET_KEYS = ("jdbc_url", "jdbc_driver", "jdbc_table")

# Fields present in the source-side required set that the target side
# deliberately does NOT require, each with the reason. Design R8.3a asks for
# exactly this discipline where two shapes legitimately differ.
DOCUMENTED_TARGET_EXCLUSIONS: dict[str, str] = {
    "jdbc_schema": (
        "The source addresses schema and table as two fields and runs "
        "validate_identifier on each. The target carries one fully-qualified "
        "`jdbc_table` (`database_name.table_name`, composed by JdbcTargetForm), "
        "so a separate `jdbc_schema` would be a second, conflicting source of "
        "truth for the same value."
    ),
    "secret_arn": (
        "C8 / invariant LGR-819980f-1, LGR-c246ab8-1. Not a mode-independent "
        "REQUEST key on the target side because `glue_connection` mode never "
        "sends it: _augment_glue_connection_config supplies it server-side from "
        "the connection's SECRET_ID, or refuses the request with a 400 when the "
        "connection has none; select_rds checks it separately, keyed on "
        "glue_connection_name. (It used to be excluded for a second reason — the "
        "target side tolerated an inline-credential connection and left it unset "
        "— which the JDBC-target credential fix withdrew.) The source side "
        "requires it in the request because the JDBC source PySpark template "
        "calls get_secret_value() at runtime."
    ),
    "engine": (
        "Resolved server-side for `glue_connection` mode (derived from the "
        "connection's JDBC URL prefix by _augment_glue_connection_config) and "
        "not needed by the target write path in the other two modes. The manual "
        "mode still requires it, via _JDBC_MANUAL_REQUIRED_FIELDS."
    ),
    "read_mode": (
        "Source-only concept — `full_table` vs `incremental`, and the watermark "
        "fields that ride along with `incremental`. A target is always written, "
        "never read."
    ),
}


# ---------------------------------------------------------------------------
# Handler mocks — nothing here may reach real AWS.
# ---------------------------------------------------------------------------


#: The Secrets Manager reference the mocked `glue_connection` connection carries
#: in its `SECRET_ID`. A reference, not a credential (LGR-819980f-1).
_CONNECTION_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:customer-owned-postgres-AbCdEf"


@pytest.fixture
def handler_mocks(monkeypatch):
    """Bind mock tables/clients onto the handler modules.

    Returns the pipelines table mock so tests can assert on persistence.
    """
    pipelines_table = MagicMock()

    monkeypatch.setattr(pipelines_crud, "pipelines_table", pipelines_table)
    monkeypatch.setattr(pipelines_crud, "state_table", MagicMock())
    monkeypatch.setattr(pipelines_crud, "executions_table", MagicMock())
    monkeypatch.setattr(pipelines_crud, "approvals_table", MagicMock())
    monkeypatch.setattr(pipelines_crud, "dynamodb", MagicMock())
    monkeypatch.setattr(pipelines_crud, "s3_client", MagicMock())

    # The handler builds fresh clients for the target-schema lookup and STS.
    # Failing the S3 lookup is the handler's documented fallback (it logs and
    # continues with the bare schema ID).
    crud_client = MagicMock()
    crud_client.get_object.side_effect = Exception("stubbed S3")
    crud_client.get_caller_identity.return_value = {"Account": "123456789012"}
    monkeypatch.setattr(pipelines_crud.boto3, "client", lambda *a, **kw: crud_client)

    # `glue_connection` mode reaches _augment_glue_connection_config, which
    # calls Glue. Return a connection whose credential is a SECRET_ID reference
    # and nothing else — the only shape the target side accepts since the
    # JDBC-target credential fix (LGR-819980f-1). This fixture used to return a
    # connection with NO SECRET_ID, the inline-credential case the target side
    # then tolerated; that tolerance is withdrawn, and the refusal is pinned by
    # `test_glue_connection_without_secret_id_is_refused` below.
    glue_client = MagicMock()
    glue_client.get_connection.return_value = {
        "Connection": {
            "Name": "customer-owned-postgres",
            "ConnectionProperties": {
                "JDBC_CONNECTION_URL": "jdbc:postgresql://customer-db.example.internal:5432/analytics",
                "SECRET_ID": _CONNECTION_SECRET_ARN,
            },
        }
    }
    glue_client.get_tags.return_value = {"Tags": {}}
    sts_client = MagicMock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    monkeypatch.setattr(
        pipelines_jdbc.boto3,
        "client",
        lambda service, *a, **kw: sts_client if service == "sts" else glue_client,
    )

    return pipelines_table


def _create(target_config: dict[str, Any]) -> dict[str, Any]:
    """Call `create_pipeline` with `target_config` and return the response."""
    with patch("handlers.pipelines_crud._provision_pipeline_resources"):
        return create_pipeline(create_pipeline_request_body(target_config))


def _persisted_target_config(pipelines_table: MagicMock) -> dict[str, Any]:
    """The `target_config` as `create_pipeline` persisted it.

    Server-side resolution — `_augment_glue_connection_config` filling in
    `engine`, the manual validator normalising `security_group_ids`, the
    `write_mode` back-fill — happens on the dict the handler parsed out of the
    request body, not on the caller's object. So the persisted record is the
    only place those effects are observable, and it is also the shape every
    downstream reader (codegen, job arguments) actually sees.
    """
    return pipelines_table.put_item.call_args.kwargs["Item"]["target_config"]


# ---------------------------------------------------------------------------
# 1. The two sides agree
# ---------------------------------------------------------------------------


class TestEnforcedKeySetIsWhatWeThinkItIs:
    """The backend's required-key set for a JDBC target, pinned.

    Validates: Requirements 9.2
    """

    def test_enforced_set_matches_the_pin(self):
        """A change to create_pipeline's JDBC branch must be a deliberate one."""
        assert ENFORCED_JDBC_TARGET_KEYS == EXPECTED_JDBC_TARGET_KEYS, (
            "create_pipeline's `target_type == 'JDBC'` branch now requires "
            f"{ENFORCED_JDBC_TARGET_KEYS}, not {EXPECTED_JDBC_TARGET_KEYS}. If "
            "that is intended, update EXPECTED_JDBC_TARGET_KEYS *and* the "
            "emitted shapes in tests/fixtures/jdbc_wizard_payloads.py together "
            "— they are the two halves of one contract."
        )

    def test_engine_and_secret_arn_are_not_required(self):
        """The deliberate exclusions, asserted rather than assumed.

        Requiring `secret_arn` in this mode-independent branch would reject the
        wizard's `glue_connection` request, which never carries it: the server
        supplies it from the connection's SECRET_ID (or refuses with a 400 when
        there is none). It no longer protects an inline-credential connection —
        that tolerance is withdrawn (LGR-819980f-1; supersedes part of
        LGR-c246ab8-1). See DOCUMENTED_TARGET_EXCLUSIONS["secret_arn"].
        """
        assert "secret_arn" not in ENFORCED_JDBC_TARGET_KEYS
        assert "engine" not in ENFORCED_JDBC_TARGET_KEYS

    def test_canonical_and_legacy_branches_enforce_the_same_set(self):
        """`JDBC` and its legacy `RDS_DATABASE` alias must not drift apart.

        `RDS_DATABASE` is normalised on read for pipelines persisted before the
        P17 consolidation. Two branches enforcing different sets would mean an
        old pipeline and a new one are validated differently.
        """
        assert _target_type_branch_required_keys("RDS_DATABASE") == ENFORCED_JDBC_TARGET_KEYS


@pytest.mark.parametrize("mode", JDBC_CONNECTION_MODES)
class TestEmittedShapeSatisfiesEnforcedSet:
    """Every mode's emitted `target_config` supplies every enforced key.

    Validates: Requirements 9.2
    """

    def test_emitted_shape_supplies_every_enforced_key(self, mode: str):
        config = emitted_jdbc_target_config(mode)
        missing = [key for key in ENFORCED_JDBC_TARGET_KEYS if not config.get(key)]
        assert not missing, (
            f"The wizard's {mode} target_config does not supply {missing}, which "
            "create_pipeline requires. Either the form stopped setting them or "
            "the backend started requiring more."
        )

    def test_emitted_shape_declares_the_canonical_type(self, mode: str):
        """`type` is always `'JDBC'`; the legacy spelling is no longer produced."""
        assert emitted_jdbc_target_config(mode)["type"] == "JDBC"

    def test_emitted_shape_declares_its_connection_mode(self, mode: str):
        """`_connection_mode` is what routes the mode-specific validation."""
        assert emitted_jdbc_target_config(mode)["_connection_mode"] == mode


# ---------------------------------------------------------------------------
# 2. The agreement is not vacuous — the real shapes round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", JDBC_CONNECTION_MODES)
class TestEmittedShapeIsAccepted:
    """Each emitted shape reaches HTTP 201 through the real handler.

    The static key comparison above could hold while the payload still failed
    for a reason no key set expresses — a transport encoding, a regex, an
    augmenter call. These tests close that gap end-to-end at the API boundary.

    Validates: Requirements 9.2
    """

    def test_emitted_shape_returns_201(self, mode: str, handler_mocks):
        response = _create(emitted_jdbc_target_config(mode))
        assert response["statusCode"] == 201, f"The wizard's {mode} target_config was rejected: {response['body']}"

    def test_emitted_shape_is_persisted(self, mode: str, handler_mocks):
        _create(emitted_jdbc_target_config(mode))
        assert handler_mocks.put_item.call_count == 1
        persisted = _persisted_target_config(handler_mocks)
        assert persisted["type"] == "JDBC"
        assert persisted["_connection_mode"] == mode
        # The enforced keys survive the round trip, so downstream codegen sees
        # concrete values rather than a shape that only passed validation.
        for key in ENFORCED_JDBC_TARGET_KEYS:
            assert persisted.get(key), f"{key} did not survive persistence in {mode} mode"


class TestTransportEncodings:
    """Shapes the wizard is forced into by its own state model.

    Validates: Requirements 9.2
    """

    def test_json_encoded_security_group_ids_are_accepted(self, handler_mocks):
        """`security_group_ids` arrives as a JSON string, and must be accepted.

        `WizardState.targetConfig` is a `Record<string, string>`, so the wizard
        cannot send a real array. Manual mode is the case that matters: it is
        the only mode where the networking validator runs, so it is the only
        mode where the encoding could be rejected.

        `_normalise_sg_list`'s own list-vs-string handling is unit-tested in
        `test_jdbc_target_manual_validator.py`. What is asserted here is that
        the encoding survives the full request path and lands normalised in the
        persisted record, which that test cannot see.
        """
        config = emitted_jdbc_target_config("manual")
        assert isinstance(config["security_group_ids"], str)

        response = _create(config)

        assert response["statusCode"] == 201, response["body"]
        # Normalised by the validator, so downstream readers see a real list.
        assert _persisted_target_config(handler_mocks)["security_group_ids"] == ["sg-0a1b2c3d4e5f6789a"]

    def test_glue_connection_mode_is_accepted_without_a_secret_arn(self, handler_mocks):
        """The wizard's `glue_connection` request carries no `secret_arn`; the server supplies it.

        The emitted `glue_connection` shape carries neither `secret_arn` nor
        `engine`. `_augment_glue_connection_config` derives `engine` from the
        connection's JDBC URL and copies `secret_arn` from its `SECRET_ID`
        (fence LGR-819980f-2: `SECRET_ID` is where the ARN lives; the
        `adi:secret_arn` tag is unreadable).

        **The persisted assertion is inverted.** This test used to assert the
        persisted record had NO `secret_arn`, for a connection with no
        SECRET_ID, because the target side then tolerated inline credentials and
        "inventing a secret_arn here would break the inline-credential path".
        That path copied the plaintext pair into Glue job arguments — the
        LGR-819980f-1 violation — and is deleted, so the request still needs no
        `secret_arn` but the record must now carry the connection's reference.
        """
        config = emitted_jdbc_target_config("glue_connection")
        assert "secret_arn" not in config
        assert "engine" not in config

        response = _create(config)

        assert response["statusCode"] == 201, response["body"]
        persisted = _persisted_target_config(handler_mocks)
        assert persisted["secret_arn"] == _CONNECTION_SECRET_ARN, (
            "The connection's SECRET_ID must be persisted as the target's secret_arn: it is "
            "the reference the Glue job is granted and reads its credentials by."
        )
        # `engine` IS resolved server-side — that is why the wizard need not send it.
        assert persisted["engine"] == "postgres"

    def test_glue_connection_without_secret_id_is_refused(self, handler_mocks, monkeypatch):
        """The withdrawn tolerance, as a contract: no SECRET_ID on the target connection is a 400.

        Nothing is persisted, and the 400 names SECRET_ID as the fix. A pipeline
        accepted here could not run: `build_jdbc_arguments` raises when no
        Secrets Manager reference resolves.
        """
        glue_client = MagicMock()
        glue_client.get_connection.return_value = {
            "Connection": {
                "Name": "customer-owned-postgres",
                "ConnectionProperties": {
                    "JDBC_CONNECTION_URL": "jdbc:postgresql://customer-db.example.internal:5432/analytics",
                },
            }
        }
        # Same mock for every service, as the fixture's own override is in effect
        # (`pipelines_crud.boto3` and `pipelines_jdbc.boto3` are one module).
        glue_client.get_caller_identity.return_value = {"Account": "123456789012"}
        monkeypatch.setattr(pipelines_jdbc.boto3, "client", lambda *a, **kw: glue_client)

        response = _create(emitted_jdbc_target_config("glue_connection"))

        assert response["statusCode"] == 400, response["body"]
        assert "target_config.secret_arn is required" in response["body"]
        assert "SECRET_ID" in response["body"]
        assert handler_mocks.put_item.call_count == 0


# ---------------------------------------------------------------------------
# 3. Side 1 of the contract — the emitted fixture is checked against the wizard
# ---------------------------------------------------------------------------
#
# Without this section the emitted half of the contract would be a transcription
# with no oracle: `JdbcConnectionSelector.handleRdsSelect` could stop setting
# `jdbc_driver` and every test above would still pass, because the fixture would
# still claim it was set. `LGR-28b4d75-5` names exactly that failure mode. The
# reader below is deliberately narrow — it understands the constructs these three
# components actually use to assign `target_config` keys: an object literal handed
# to `update(...)` / `onChange(...)`, and `JdbcTargetForm`'s `merged.<key> = ...`
# assignments. It raises on anything it does not recognise rather than returning a
# short set, because a silently-empty key set makes every assertion built on it
# vacuously true.
#
# It lives here rather than in `helpers.py` because `helpers.py` reads
# `export interface` declarations and has one documented reason to hold a
# parser: a second endpoint needed it. There is no second consumer of this one
# yet; lift it out when there is.

_WIZARD_DIR = _REPO_ROOT / "dashboard-react" / "src" / "pages" / "wizard"
_CONNECTION_SELECTOR = _WIZARD_DIR / "JdbcConnectionSelector.tsx"
_MANUAL_FORM = _WIZARD_DIR / "ManualConnectionForm.tsx"
_TARGET_FORM = _WIZARD_DIR / "JdbcTargetForm.tsx"


def _read_tsx(path: pathlib.Path) -> str:
    assert path.is_file(), (
        f"{path} is missing. This contract test derives the emitted key set from that component; "
        "if it moved, repoint the constant rather than deleting the assertion."
    )
    return path.read_text(encoding="utf-8")


def _balanced_block(source: str, open_index: int) -> str:
    """Return the `{...}` block starting at `open_index`, braces balanced."""
    assert source[open_index] == "{", f"expected '{{' at {open_index}, got {source[open_index]!r}"
    depth = 0
    for i in range(open_index, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[open_index : i + 1]
    raise AssertionError(f"unbalanced braces from offset {open_index}")


def _function_body(source: str, name: str, path: pathlib.Path) -> str:
    """The body of `function <name>(...) { ... }`."""
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", source)
    assert match, (
        f"{name} not found in {path} — that function is the point where this "
        "connection mode's target_config keys are assigned, so the emitted set "
        "cannot be derived without it."
    )
    return _balanced_block(source, match.end() - 1)


def _strip_comments(source: str) -> str:
    """Remove `//` and `/* */` comments, so a comma in prose cannot split a key."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def _literal_keys(block: str) -> tuple[set[str], set[str]]:
    """Top-level keys of an object literal: `(string_keys, computed_key_vars)`.

    Splits on top-level commas with brace/bracket/paren depth tracking, so it is
    indifferent to whether the literal is written on one line or twelve. Spreads
    (`...config`) are skipped: they carry existing keys forward rather than
    assigning new ones. Shorthand (`engine,`) counts as an assignment.

    A computed key (`[idKey]: ...`) yields the *variable name* for the caller to
    resolve; accepting it as an opaque key would silently drop a real field. An
    unrecognised segment raises — a half-parsed key set is worse than none.
    """
    inner = _strip_comments(block)[1:-1]
    literal: set[str] = set()
    computed: set[str] = set()

    segments: list[str] = []
    depth = 0
    start = 0
    for i, char in enumerate(inner):
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == "," and depth == 0:
            segments.append(inner[start:i])
            start = i + 1
    segments.append(inner[start:])

    for segment in segments:
        text = segment.strip()
        if not text or text.startswith("..."):
            continue
        if key := re.match(r"^(\w+)\s*:", text):
            literal.add(key.group(1))
        elif computed_key := re.match(r"^\[(\w+)\]\s*:", text):
            computed.add(computed_key.group(1))
        elif shorthand := re.fullmatch(r"(\w+)", text):
            literal.add(shorthand.group(1))  # `{ engine, jdbc_driver: ... }`
        else:
            raise AssertionError(
                f"Unparsed object-literal entry {text!r} — teach _literal_keys about it "
                "rather than letting a key go uncounted."
            )
    return literal, computed


def _object_literal_keys_after(source: str, pattern: str) -> tuple[set[str], set[str]]:
    """Union of the keys of every object literal whose `{` follows `pattern`."""
    literal: set[str] = set()
    computed: set[str] = set()
    for match in re.finditer(pattern, source):
        block_literal, block_computed = _literal_keys(_balanced_block(source, match.end() - 1))
        literal |= block_literal
        computed |= block_computed
    return literal, computed


def _config_patch_keys(source: str) -> tuple[set[str], set[str]]:
    """Keys assigned by `update({...})` / `onChange({...})` in a config-level component.

    Valid for `JdbcConnectionSelector` and `ManualConnectionForm`, whose `update`
    and `onChange` both take the connection config itself. NOT valid for
    `JdbcTargetForm`, whose `onChange` takes the whole `WizardState` — see
    `_target_form_keys`.
    """
    return _object_literal_keys_after(source, r"\b(?:update|onChange)\(\s*\{")


def _target_form_keys(source: str) -> set[str]:
    """The `target_config` keys `JdbcTargetForm` assigns, for every mode.

    Three distinct assignment forms, and all three have to be read or the set is
    wrong: `update({...})` patches (which `JdbcTargetForm.update` merges into
    `targetConfig`), nested `targetConfig: {...}` literals passed straight to
    `onChange`, and direct `merged.<key> =` assignments. The form's *top-level*
    `onChange({...})` keys are `WizardState` fields (`targetConfig`,
    `detectedTargetSchema`) and are deliberately not collected.
    """
    patch_literal, patch_computed = _object_literal_keys_after(source, r"\bupdate\(\s*\{")
    nested_literal, nested_computed = _object_literal_keys_after(source, r"\btargetConfig:\s*\{")
    assert not (patch_computed | nested_computed), (
        f"unexpected computed keys in {_TARGET_FORM}: {patch_computed | nested_computed}"
    )
    assigned = set(re.findall(r"\bmerged\.(\w+)\s*=", _strip_comments(source)))
    assert assigned, (
        f"{_TARGET_FORM} no longer assigns target_config keys as `merged.<key> = ...`. "
        "`type` and `jdbc_table` were set that way, and jdbc_table's derivation is the "
        "only reason a JDBC target satisfies the backend's jdbc_table requirement — "
        "update this reader rather than dropping the keys."
    )
    return patch_literal | nested_literal | assigned


def _resolve_ternary_alternatives(body: str, var_name: str) -> set[str]:
    """String alternatives of `const <var_name> = cond ? 'a' : 'b';`."""
    match = re.search(
        re.escape(var_name) + r"\s*=\s*[^;]*?\?\s*'([^']+)'\s*:\s*'([^']+)'",
        body,
    )
    assert match, (
        f"{var_name} is used as a computed target_config key but its value could "
        "not be resolved. Teach this reader about the new form rather than "
        "dropping the key."
    )
    return {match.group(1), match.group(2)}


#: The selector function that populates each non-manual mode's connection fields.
_MODE_HANDLERS = {"select_rds": "handleRdsSelect", "glue_connection": "handleGlueConnectionSelect"}


def _mode_handler_keys(mode: str) -> set[str]:
    """Keys the mode's own auto-population handler assigns, and only those.

    Narrower than `_wizard_emitted_keys` on purpose. The selector also renders
    free-text JDBC URL / Driver / Port inputs for both non-manual modes, so a key
    can have a "wizard origin" while only ever being typed by hand. For the two
    discovery modes that is not the promise: the point of picking an RDS instance
    or a Glue Connection is that the connection fields are derived for you.
    """
    body = _function_body(_read_tsx(_CONNECTION_SELECTOR), _MODE_HANDLERS[mode], _CONNECTION_SELECTOR)
    literal, computed = _object_literal_keys_after(body, r"\bupdate\(\s*\{")
    for var_name in computed:
        literal |= _resolve_ternary_alternatives(body, var_name)
    return literal


def _wizard_emitted_keys(mode: str) -> set[str]:
    """The `target_config` keys the wizard assigns for `mode`, read from the TSX.

    Union of the always-on `JdbcTargetForm` keys and the mode-specific
    assignment point: `handleRdsSelect` / `handleGlueConnectionSelect` in
    `JdbcConnectionSelector`, or the whole of `ManualConnectionForm`.

    Keys the selector's shared inputs can set (`jdbc_url`, `jdbc_driver`,
    `_port`, and the select_rds-only `secret_arn` dropdown) are included for the
    two non-manual modes: they are in that file's `update({...})` calls and the
    reader does not try to model which JSX branch renders them. Over-collecting
    is the safe direction — it can only demand that the fixture account for
    more, never less.
    """
    selector = _read_tsx(_CONNECTION_SELECTOR)

    # JdbcTargetForm's keys are set in every mode.
    keys = _target_form_keys(_read_tsx(_TARGET_FORM))
    # Stamped by the selector's handleModeChange on every transition.
    keys.add("_connection_mode")

    if mode == "manual":
        manual_literal, manual_computed = _config_patch_keys(_read_tsx(_MANUAL_FORM))
        assert not manual_computed, f"unexpected computed keys in {_MANUAL_FORM}: {manual_computed}"
        keys |= manual_literal
    else:
        keys |= _mode_handler_keys(mode)
        # The selector's shared inputs (JDBC URL / Driver / Port) and the
        # select_rds-only secret dropdown also write to the config, and the
        # reader does not model which JSX branch renders which. Collect them
        # for both non-manual modes and let _OVER_COLLECTED carry the
        # per-mode exemptions.
        shared_literal, _ = _config_patch_keys(selector)
        keys |= shared_literal

    # Reader hygiene: never let a parse failure look like agreement.
    assert len(keys) > 3, f"suspiciously few emitted keys parsed for {mode}: {sorted(keys)}"
    return keys


#: Keys the reader collects that a given mode does not actually emit, because
#: the JSX branch that sets them does not render in that mode. Each is a
#: deliberate over-collection by `_wizard_emitted_keys`, listed so the
#: exemption is visible rather than buried in the reader.
_OVER_COLLECTED: dict[str, dict[str, str]] = {
    "select_rds": {
        "glue_connection_name": (
            "Set only by handleGlueConnectionSelect, and handleModeChange deletes it on entering this mode."
        ),
        "db_cluster_identifier": (
            "The Aurora spelling of the identifier key — handleRdsSelect picks one or the "
            "other from `selected.isCluster`, never both. The fixture models an instance."
        ),
    },
    "glue_connection": {
        "_selected_rds": "select_rds-private; deleted by handleModeChange on entering this mode.",
        "_port": "The Port input is rendered for both non-manual modes but nothing populates it here.",
        "engine": (
            "Not surfaced in this mode — resolved server-side by "
            "_augment_glue_connection_config from the connection's JDBC URL prefix. C8."
        ),
        "secret_arn": (
            "The secret dropdown renders only under `connectionMode === 'select_rds'`. On this "
            "path the connection's own SECRET_ID is authoritative and the server copies it in; "
            "a connection without one is refused (it used to be tolerated as inline-credential). "
            "C8 / LGR-819980f-1, LGR-819980f-2."
        ),
        "subnet_id": (
            "select_rds derives networking from the described RDS instance; a user's existing "
            "Glue Connection already carries its own (P20 Req 7.6)."
        ),
        "security_group_ids": "As subnet_id.",
        "availability_zone": "As subnet_id.",
    },
}


@pytest.mark.parametrize("mode", JDBC_CONNECTION_MODES)
class TestEmittedFixtureTracksTheWizard:
    """The fixture accounts for every key the wizard components assign.

    This is what stops the emitted half of the contract being a transcription
    that agrees only with itself (``LGR-28b4d75-5``). It failed once already:
    the `manual` shape was missing `vpc_id`, which `ManualConnectionForm`
    auto-selects.

    Validates: Requirements 9.2
    """

    def test_fixture_accounts_for_every_key_the_wizard_assigns(self, mode: str):
        emitted = set(emitted_jdbc_target_config(mode))
        from_wizard = _wizard_emitted_keys(mode)
        exempt = set(_OVER_COLLECTED.get(mode, {}))

        unaccounted = from_wizard - emitted - exempt
        assert not unaccounted, (
            f"The wizard assigns {sorted(unaccounted)} in {mode} mode but "
            "tests/fixtures/jdbc_wizard_payloads.py does not carry them. Either "
            "add them to the fixture, or — if the JSX branch that sets them does "
            f"not render in {mode} mode — record them in _OVER_COLLECTED with "
            "the reason."
        )

    def test_no_exemption_is_stale(self, mode: str):
        """An exemption that no longer suppresses anything is a door left open.

        Same discipline as `test_every_source_only_field_has_a_documented_reason`
        below: an unused entry means either the reader changed or the wizard did,
        and either way the note has stopped describing reality.
        """
        from_wizard = _wizard_emitted_keys(mode)
        emitted = set(emitted_jdbc_target_config(mode))
        stale = {key for key in _OVER_COLLECTED.get(mode, {}) if key not in from_wizard - emitted}
        assert not stale, (
            f"{sorted(stale)} is listed in _OVER_COLLECTED for {mode} mode but is not "
            "actually over-collected — either the fixture now carries it or the wizard "
            "no longer assigns it. Remove the entry."
        )

    def test_the_enforced_keys_are_ones_the_wizard_actually_sets(self, mode: str):
        """The three keys `create_pipeline` requires must have a wizard origin.

        The round-trip tests above prove the *fixture* satisfies the backend.
        This proves the fixture is not the only thing that would: each enforced
        key is assigned by a component the user drives, so the 201 is evidence
        about the product and not about this file.
        """
        from_wizard = _wizard_emitted_keys(mode)
        missing = [key for key in ENFORCED_JDBC_TARGET_KEYS if key not in from_wizard]
        assert not missing, (
            f"No wizard component assigns {missing} in {mode} mode, yet "
            "create_pipeline requires them — a JDBC target configured this way "
            "would be rejected with a 400 the user cannot clear from the form."
        )


@pytest.mark.parametrize("mode", sorted(_MODE_HANDLERS))
class TestDiscoveryModesDeriveTheConnectionFields:
    """The two discovery modes populate `jdbc_url` and `jdbc_driver` themselves.

    Separate from the test above because the selector also renders free-text
    JDBC URL / Driver / Port inputs whenever the mode is not `manual`. Those
    inputs mean an enforced key can have a wizard origin while still only ever
    being typed by hand — which satisfies "the wizard can set it" and defeats
    the actual promise of picking an RDS instance or a Glue Connection.

    This is also where the 0.5 finding's residual risk lands: `handleRdsSelect`
    sets `jdbc_driver: mapping?.driver ?? ''`, so an engine absent from
    `JDBC_DRIVER_MAP` yields an empty driver and a 400 from the branch 4.2
    restored. The key being assigned at all is the precondition for that
    fallback being reachable; if the assignment goes away the mode stops
    deriving anything and the 400 becomes unconditional.

    Validates: Requirements 9.2
    """

    def test_handler_populates_the_connection_fields(self, mode: str):
        derived = _mode_handler_keys(mode)
        for key in ("jdbc_url", "jdbc_driver"):
            assert key in derived, (
                f"{_MODE_HANDLERS[mode]} no longer derives {key}. The mode's shared "
                "free-text inputs would still let a user type it, so nothing else here "
                f"fails — but {mode} mode exists precisely so they do not have to."
            )

    def test_handler_stamps_its_own_mode(self, mode: str):
        """The handler sets `_connection_mode`, which routes the backend validation."""
        assert "_connection_mode" in _mode_handler_keys(mode)


# ---------------------------------------------------------------------------
# 4. Source/target parity — the asymmetry, stated and tested
# ---------------------------------------------------------------------------


class TestSourceTargetParity:
    """The source and target required-field sets differ. On purpose, and by how much.

    The source side requires a full common set in every connection mode via
    `_validate_jdbc_source_required_common`. The target side requires three
    fields in every mode, plus manual-mode networking. Before p31 that
    difference was something a reader had to reconstruct from two files, and the
    reconstruction was wrong — the comment at the dispatch site cited the
    coverage as living in a branch that no longer ran. Asserting it is how it
    stops being accidental.

    Validates: Requirements 9.2, 9.3
    """

    def test_target_required_set_is_a_subset_of_the_source_set(self):
        """The target asks for strictly less than the source, never something else."""
        extra = set(ENFORCED_JDBC_TARGET_KEYS) - set(_JDBC_SOURCE_REQUIRED_FIELDS)
        assert not extra, (
            f"The target now requires {sorted(extra)}, which the source does not. "
            "A field required on one side only is either a real asymmetry that "
            "belongs in DOCUMENTED_TARGET_EXCLUSIONS with its reason, or a bug."
        )

    def test_every_source_only_field_has_a_documented_reason(self):
        """No source-side field may be absent from the target set silently."""
        source_only = set(_JDBC_SOURCE_REQUIRED_FIELDS) - set(ENFORCED_JDBC_TARGET_KEYS)
        undocumented = source_only - set(DOCUMENTED_TARGET_EXCLUSIONS)
        assert not undocumented, (
            f"{sorted(undocumented)} is required of a JDBC source but not of a "
            "JDBC target, with no reason recorded. Add it to "
            "DOCUMENTED_TARGET_EXCLUSIONS with the reason, or require it on the "
            "target too."
        )

        stale = set(DOCUMENTED_TARGET_EXCLUSIONS) - source_only
        assert not stale, (
            f"{sorted(stale)} is documented as a target-side exclusion but is no "
            "longer source-only — the note is stale and misleading."
        )

    def test_manual_mode_requirements_are_symmetric(self):
        """Manual mode is the one place the two sides are identical, by requirement.

        P20 Req 6.5 says the manual-mode field names are the same on both sides
        and that the side is determined only by which config dict the validator
        is handed. One shared constant is how that is implemented; this asserts
        the constant is still shared and still says what R6.5 says.
        """
        assert _JDBC_MANUAL_REQUIRED_FIELDS == (
            "engine",
            "jdbc_url",
            "jdbc_driver",
            "secret_arn",
            "subnet_id",
            "security_group_ids",
        )
        # `availability_zone` is intentionally absent — Glue derives it from
        # the subnet when omitted.
        assert "availability_zone" not in _JDBC_MANUAL_REQUIRED_FIELDS

    def test_manual_mode_does_not_cover_the_table_identity(self):
        """Manual mode's validator has nothing to say about `jdbc_table`.

        Which is why the branch checks matter for all three modes rather than
        only the two without a validator: even in manual mode, `jdbc_table` is
        enforced by the target-type branch and nowhere else.
        """
        assert "jdbc_table" in ENFORCED_JDBC_TARGET_KEYS
        assert "jdbc_table" not in _JDBC_MANUAL_REQUIRED_FIELDS
