# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p64 2c.1 — ``create_pipeline`` persists a submitted Quality_Config (R7.1/R7.3, C7).

The Quality_Config is a ``quality_config`` sub-object, a top-level **peer** of
``approval_config`` on the pipeline record. Design C7 rejects the cheap route
explicitly: ``code_assembly.generate_code`` does
``template_vars.update(target_config)``, so a flag stored inside ``target_config``
would become a template variable with **zero** backend change — and
``target_config`` is a free-form ``Record<string, string>`` with no validation, so
it is a place to hide an unvalidated boolean. A data-quality setting is not target
configuration.

R7.3 is the requirement that makes this a create-path change rather than an
edit-path one: the setting must be settable **at pipeline creation**. The trap R7.3
names — ``create_pipeline`` assigning ``approval_config`` unconditionally and
discarding whatever the wizard sent — was fixed by p64 task 2a.1, so this copies the
**fixed** pattern. Its four parts, restated because this module asserts each of
them:

1. the defaults are a **function** returning a fresh dict, not a module constant;
2. **membership** decides whether they apply — ``key not in data``, never
   ``if data.get(key):``, which reads ``{}`` as absence;
3. any dict of the right container type persists **verbatim**;
4. a wrong container type is refused by the validator (a 400).

Expectations are derived from R7, not from what the handler emits. Everything here
**fails against unfixed code**, which had no ``quality_config`` writer at all: the
key was absent from every record and ``code_assembly.resolve_data_quality_enabled``
— built read-only by 2b.4 — resolved it for a record nothing wrote.

THE ONE DELIBERATE ASYMMETRY WITH THE APPROVAL_CONFIG, and it is the assertion
worth reading first. ``_default_quality_config`` seeds ``dedup_enabled`` and
``dedup_columns`` and **not** ``data_quality_enabled``. That key's default is *per
template* — ``DQ_DEFAULT_ON_TEMPLATES`` is on for ``glue_job_s3_tables.py.j2`` and
off for the other four, i.e. the posture each had before p64 — so seeding ``False``
would write an explicit opt-OUT onto every newly created S3 Tables pipeline,
removing a row-level split and quarantine write that target type has had since P28
and moving its verdict from ``enforced`` to ``advisory``. That is R6.6's own
prohibited reclassification, running the other way.
``TestTheSeedDoesNotDecideTheDqGate`` pins it.

Validates: Requirements 7.1, 7.2, 7.3
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# Stub out Lambda-layer dependencies before importing, matching the sibling
# ``test_create_pipeline_approval_config.py``.
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = MagicMock()
sys.modules["env_utils"].require_env = lambda name, default=None: default or name

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

try:
    from handlers.pipelines import create_pipeline

    # Imported directly because ``create_pipeline`` no longer reaches arm 3 — the
    # boundary refusal 400s a non-dict first — so the branch has to be pinned at the
    # function. Same reason the sibling module imports its own arm-3 function.
    from handlers.pipelines_crud import _default_quality_config, _submitted_quality_config

    # The names are asserted through the SHARED constants rather than restated as
    # literals: they are the whole mechanism keeping the writer here and the reader in
    # ``create_code`` from drifting (``LGR-eee8ac8-5``), so a test that spelled them out
    # again could pass while the two sides disagreed.
    from quality_verdict import (
        DATA_QUALITY_ENABLED_KEY,
        DEDUP_COLUMNS_KEY,
        DEDUP_ENABLED_KEY,
        DQ_DEFAULT_ON_TEMPLATES,
        NONPRINTABLE_CHECK_ENABLED_KEY,
        QUALITY_CONFIG_KEY,
    )
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

_PIPELINES_CRUD_SOURCE = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_dashboard_api",
    "handlers",
    "pipelines_crud.py",
)


def _body(**overrides):
    """The valid create body, with ``quality_config`` added only when asked for.

    Passed through :func:`dict.update`, so an explicit ``quality_config={}``
    produces a body that **has** the key with an empty value — arm 2, which must
    not re-default. Omitting the keyword produces a body with no key: arm 1.
    """
    payload = {
        "pipeline_name": "Test Pipeline",
        "target_schema": "uploaded_test_schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "description": "A test pipeline",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _mocked_clients(mock_boto3):
    """Stub the Lambda client for provisioning and make S3 ``get_object`` raise.

    The schema-loading branch then falls to its ``except`` and the request still
    returns 201, by ``LGR-cad8032-2`` — an unresolvable schema id writes no address
    attributes and does **not** 400. Mirrors the sibling module's wiring.
    """
    mock_lambda = MagicMock()
    mock_lambda.invoke.return_value = {"Payload": MagicMock(read=lambda: json.dumps({"status": "ok"}).encode())}
    mock_boto3.client.side_effect = lambda svc, **kw: (
        mock_lambda
        if svc == "lambda"
        else MagicMock(
            get_object=MagicMock(side_effect=Exception("skip")),
            get_caller_identity=MagicMock(return_value={"Account": "123456789012"}),
        )
    )


def _persisted_item(body):
    """Run ``create_pipeline`` against mocks and return the item handed to ``put_item``."""
    with (
        patch("handlers.pipelines_crud.boto3") as mock_boto3,
        patch("handlers.pipelines_crud.pipelines_table") as mock_table,
    ):
        _mocked_clients(mock_boto3)

        response = create_pipeline(body)
        assert response["statusCode"] == 201, response

        mock_table.put_item.assert_called_once()
        return mock_table.put_item.call_args[1]["Item"]


def _refusal(body):
    """Run ``create_pipeline`` and return ``(response, table mock)`` without asserting a status.

    The sibling of :func:`_persisted_item` for the arm that must not reach
    ``put_item``. It states no status code of its own, so a test asserts the 400
    itself, and it hands back the table mock so the caller can assert nothing was
    written — which is the substantive half. A 400 that still created the record
    would pass a status-code-only assertion.
    """
    with (
        patch("handlers.pipelines_crud.boto3") as mock_boto3,
        patch("handlers.pipelines_crud.pipelines_table") as mock_table,
    ):
        _mocked_clients(mock_boto3)
        return create_pipeline(body), mock_table


# ---------------------------------------------------------------------------
# Arm 1 — absence selects the defaults
# ---------------------------------------------------------------------------


class TestOmittedQualityConfigGetsDefaults:
    """**Validates: Requirements 7.1**"""

    def test_absent_key_seeds_the_dedup_defaults(self):
        """A body with no ``quality_config`` gets the seeded map, and dedup is OFF.

        R7.1's "MUST default to **off** so no existing pipeline's behaviour
        changes", asserted as a whole-map equality so a *silent addition* to the
        seed is caught too — which matters here more than usual, because the one
        key that must NOT be added is the subject of
        :class:`TestTheSeedDoesNotDecideTheDqGate`.
        """
        assert _persisted_item(_body())[QUALITY_CONFIG_KEY] == {
            DEDUP_ENABLED_KEY: False,
            DEDUP_COLUMNS_KEY: [],
        }

    def test_the_default_map_is_a_fresh_object_per_call(self):
        """Two creates do not share one mutable default map.

        ``_default_quality_config`` is a function returning a literal rather than a
        module constant precisely so this holds: the result is handed to
        ``put_item`` as a nested map, and a shared dict is the kind of thing a
        later caller edits in place. Part 1 of the four-part pattern.
        """
        first = _default_quality_config()
        second = _default_quality_config()

        assert first == second
        assert first is not second

        first[DEDUP_ENABLED_KEY] = True
        assert _default_quality_config()[DEDUP_ENABLED_KEY] is False


class TestTheSeedDoesNotDecideTheDqGate:
    """The deliberate asymmetry with ``_default_approval_config`` (R6.6, both directions).

    **Validates: Requirements 7.1**
    """

    def test_the_seed_carries_no_data_quality_enabled(self):
        """Seeding it would opt every new S3 Tables pipeline OUT of its DQ block.

        ``resolve_data_quality_enabled`` honours an explicit ``bool`` and otherwise
        falls back to ``DQ_DEFAULT_ON_TEMPLATES``. A seeded ``False`` here IS an
        explicit bool, so it would win over that fallback — stripping the row-level
        split and quarantine write from every newly created S3 Tables pipeline and
        moving its verdict from ``enforced`` to ``advisory``. R6.6 forbids exactly
        that, and 2b.4's docstring names a flat default as the reason its own is
        per-template.
        """
        assert DATA_QUALITY_ENABLED_KEY not in _default_quality_config(), (
            f"{DATA_QUALITY_ENABLED_KEY!r} is seeded by _default_quality_config. Its "
            f"default is PER TEMPLATE (on for {sorted(DQ_DEFAULT_ON_TEMPLATES)}, off for "
            f"the rest), so an explicit value here overrides that fallback and silently "
            f"reclassifies a pipeline whose behaviour nobody changed. Leave it absent and "
            f"let code_assembly.resolve_data_quality_enabled answer."
        )

    def test_a_created_record_does_not_claim_a_dq_posture(self):
        """The same thing asserted at the record, not just at the seed function.

        A future writer could reintroduce the key past ``_default_quality_config``
        — in the persist block, or by merging the seed with something. This catches
        that too, so the guarantee is about what lands in DynamoDB.
        """
        assert DATA_QUALITY_ENABLED_KEY not in _persisted_item(_body())[QUALITY_CONFIG_KEY]

    def test_an_explicit_opt_in_is_still_persisted(self):
        """The key is absent by DEFAULT, not unwritable — otherwise 2b.4's gate is dead.

        The paired positive, without which the two assertions above could be
        satisfied by dropping the key altogether and leaving the DQ opt-in
        unsettable, which is the whole thing 2c.1 exists to fix.
        """
        submitted = {DATA_QUALITY_ENABLED_KEY: True}
        assert _persisted_item(_body(quality_config=submitted))[QUALITY_CONFIG_KEY] == submitted


# ---------------------------------------------------------------------------
# Arm 2 — a submitted dict persists verbatim
# ---------------------------------------------------------------------------


class TestSubmittedQualityConfigPersists:
    """**Validates: Requirements 7.1, 7.2, 7.3**"""

    def test_a_supplied_map_is_persisted_as_supplied(self):
        """R7.3: a wizard-supplied setting survives the create path."""
        submitted = {
            DATA_QUALITY_ENABLED_KEY: True,
            DEDUP_ENABLED_KEY: True,
            DEDUP_COLUMNS_KEY: ["patient_id", "visit_date"],
        }

        assert _persisted_item(_body(quality_config=submitted))[QUALITY_CONFIG_KEY] == submitted

    def test_an_explicitly_empty_map_is_not_re_defaulted(self):
        """Part 2: membership decides, not truthiness.

        ``{}`` is present-and-empty, which is an explicit "no overrides" and must be
        recorded as one. A truth test — ``if data.get(QUALITY_CONFIG_KEY):`` — would
        read it as absence and silently upgrade it to the seeded map. That is the
        ``${VAR:-default}`` substitutes-on-empty class of bug, which p64 task 1d.4
        hit one Lambda over.
        """
        assert _persisted_item(_body(quality_config={}))[QUALITY_CONFIG_KEY] == {}

    def test_an_all_false_map_is_not_re_defaulted(self):
        """The same distinction one step in: every value falsy, the dict itself truthy."""
        submitted = {DATA_QUALITY_ENABLED_KEY: False, DEDUP_ENABLED_KEY: False}

        assert _persisted_item(_body(quality_config=submitted))[QUALITY_CONFIG_KEY] == submitted

    def test_an_unrecognised_key_is_persisted_rather_than_filtered(self):
        """Part 3: verbatim means verbatim — no allowlist.

        Key filtering is a different obligation from value validation, and an
        allowlist would make a stale Lambda 400 a newer wizard. Deployment drift is
        a measured property of this project rather than a hypothesis
        (``LGR-c88d326-4``).
        """
        submitted = {DEDUP_ENABLED_KEY: True, "nonprintable_check_enabled": True}

        assert _persisted_item(_body(quality_config=submitted))[QUALITY_CONFIG_KEY] == submitted

    def test_a_float_under_an_unrecognised_key_is_converted_for_dynamodb(self):
        """Serialisation, not validation — and the reason it is not hypothetical.

        No recognised key carries a number today, but arm 2 persists verbatim
        INCLUDING unrecognised keys, ``json.loads`` yields ``float`` for a JSON
        number with a fractional part, and boto3 refuses ``float``. Without the
        shared converter a forward-compatible client sending one key this Lambda
        does not know about would take the whole create down with a ``TypeError`` on
        ``put_item``.
        """
        persisted = _persisted_item(_body(quality_config={"dedup_similarity_threshold": 0.85}))[QUALITY_CONFIG_KEY]

        assert persisted == {"dedup_similarity_threshold": Decimal("0.85")}
        assert isinstance(persisted["dedup_similarity_threshold"], Decimal)


# ---------------------------------------------------------------------------
# Arm 3 — the backstop the handler no longer reaches
# ---------------------------------------------------------------------------


class TestArmThreeSurvivesAsABackstop:
    """A non-dict writes NO attribute, pinned at the function.

    ``create_pipeline`` cannot reach this arm — ``_validate_quality_config`` 400s a
    non-dict several hundred lines earlier — so it is asserted directly. It is kept
    rather than deleted because the function's contract is its own: every reader does
    ``record.get("quality_config", {}).get(key, default)``, so a stored scalar makes
    the *reader* raise ``AttributeError`` several Lambdas away.

    **Validates: Requirements 7.1**
    """

    @pytest.mark.parametrize("submitted", ["on", ["dedup"], 1, 0, True, False, None])
    def test_a_non_dict_container_writes_nothing(self, submitted):
        assert _submitted_quality_config({QUALITY_CONFIG_KEY: submitted}) is None

    def test_an_absent_key_still_reaches_the_defaults(self):
        """The paired positive, so the parametrized case above is not vacuous."""
        assert _submitted_quality_config({}) == {
            DEDUP_ENABLED_KEY: False,
            DEDUP_COLUMNS_KEY: [],
        }


# ---------------------------------------------------------------------------
# R10.4's boundary validation — the item 2b.4 handed forward
# ---------------------------------------------------------------------------


class TestBoundaryValidation:
    """A Quality_Config that cannot mean what it says is a 400.

    2b.4 called its own resolver's ``isinstance(explicit, bool)`` check "a floor,
    not a substitute", and this is the substitute. The floor alone is not enough for
    a concrete reason: on an S3 Tables pipeline a non-boolean
    ``data_quality_enabled`` falls through to the per-template default of **on**, so
    an operator who submitted a string meaning "off" would get DQ on with no error
    anywhere.

    **Validates: Requirements 7.1, 7.2**
    """

    @pytest.mark.parametrize("submitted", ["on", ["dedup"], 42])
    def test_a_non_dict_quality_config_is_refused(self, submitted):
        response, mock_table = _refusal(_body(quality_config=submitted))

        assert response["statusCode"] == 400
        assert QUALITY_CONFIG_KEY in json.loads(response["body"])["error"]
        mock_table.put_item.assert_not_called()

    @pytest.mark.parametrize(
        "key",
        [DATA_QUALITY_ENABLED_KEY, DEDUP_ENABLED_KEY, NONPRINTABLE_CHECK_ENABLED_KEY],
    )
    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
    def test_a_non_boolean_opt_in_is_refused(self, key, value):
        """All three gates, and `1`/`0` too — ``isinstance(1, bool)`` is False by design.

        ``nonprintable_check_enabled`` joined the other two in p64 task 2d.3. It is
        the weakest of the three cases on its own: its resolver returns ``False``
        for a non-boolean, so a malformed value cannot turn the check **on**, which
        is why 2d.2 left it out and said so. It is refused here because failing
        closed is still an opt-in that silently does nothing, and because two
        sibling keys refusing a bad boolean while the third accepted one is the
        inconsistency a reader of this validator would trip over.
        """
        response, mock_table = _refusal(_body(quality_config={key: value}))

        assert response["statusCode"] == 400, response
        assert key in json.loads(response["body"])["error"]
        mock_table.put_item.assert_not_called()

    @pytest.mark.parametrize("value", [True, False])
    def test_a_real_nonprintable_boolean_is_accepted_and_persisted(self, value):
        """The paired positive for the key 2d.3 added: only the *non*-boolean is refused.

        Asserted on the persisted record rather than on the 201, because the point of
        refusing the string form is that the real boolean survives to the attribute
        the resolver in the other Lambda reads.
        """
        persisted = _persisted_item(_body(quality_config={NONPRINTABLE_CHECK_ENABLED_KEY: value}))

        assert persisted[QUALITY_CONFIG_KEY][NONPRINTABLE_CHECK_ENABLED_KEY] is value

    @pytest.mark.parametrize("value", [True, False])
    def test_a_real_boolean_opt_in_is_accepted(self, value):
        """The paired positive: only the *non*-boolean is refused."""
        assert _persisted_item(_body(quality_config={DEDUP_ENABLED_KEY: value}))[QUALITY_CONFIG_KEY] == {
            DEDUP_ENABLED_KEY: value
        }

    @pytest.mark.parametrize("columns", ["patient_id", {"0": "patient_id"}, 7])
    def test_a_non_list_dedup_columns_is_refused(self, columns):
        response, mock_table = _refusal(_body(quality_config={DEDUP_COLUMNS_KEY: columns}))

        assert response["statusCode"] == 400, response
        assert DEDUP_COLUMNS_KEY in json.loads(response["body"])["error"]
        mock_table.put_item.assert_not_called()

    @pytest.mark.parametrize(
        "column",
        [
            'id"); import os; os.system("sh',  # the injection this closes
            "patient id",  # a space is not an identifier
            "patient-id",  # nor a hyphen
            "1_id",  # nor a leading digit
            "",  # nor empty
            None,  # nor a non-string
            "x" * 256,  # nor over the 255 cap
        ],
    )
    def test_a_dedup_column_that_is_not_an_identifier_is_refused(self, column):
        """R7.2 / R8.5: these names reach ``dropDuplicates([...])`` in generated code.

        Refused, never filtered or sanitised. Dropping the offending entry instead
        was rejected for ``LGR-4ea43f3-4``'s reason one value class over: a silently
        altered value is well-formed, plausible and wrong, and a dedup key list
        quietly missing a column deduplicates on the wrong identity. do-NOT 2
        forbids weakening the allowlist at a sink; do-NOT 3 forbids modifying the
        validator, and nothing here does either — it is only called.
        """
        response, mock_table = _refusal(_body(quality_config={DEDUP_COLUMNS_KEY: [column]}))

        assert response["statusCode"] == 400, response
        assert DEDUP_COLUMNS_KEY in json.loads(response["body"])["error"]
        mock_table.put_item.assert_not_called()

    def test_conforming_dedup_columns_are_accepted_and_the_empty_list_is_legal(self):
        """The paired positive, both shapes.

        An empty list is the R7.2 default — "all mapped target columns", which is
        what ``dropDuplicates()`` with no argument already does — so it must pass.
        And a conforming list must pass, or the refusals above are just a ban.
        """
        assert _persisted_item(_body(quality_config={DEDUP_COLUMNS_KEY: []}))[QUALITY_CONFIG_KEY] == {
            DEDUP_COLUMNS_KEY: []
        }
        assert _persisted_item(_body(quality_config={DEDUP_COLUMNS_KEY: ["patient_id", "visit_date"]}))[
            QUALITY_CONFIG_KEY
        ] == {DEDUP_COLUMNS_KEY: ["patient_id", "visit_date"]}

    def test_an_omitted_quality_config_is_not_a_malformed_one(self):
        """Absence is the compatibility arm and must not 400."""
        assert QUALITY_CONFIG_KEY in _persisted_item(_body())


# ---------------------------------------------------------------------------
# The cross-Lambda contract, and the casing convention
# ---------------------------------------------------------------------------


class TestTheAttributeNameHasOneDefinition:
    """No second literal in the writer — the reader lives in another Lambda.

    ``create_pipeline`` writes ``quality_config``; ``code_assembly`` reads it. The
    two are only ever compared through these strings, so a second spelling here
    would not fail loudly — it would make every opt-in silently never match, and an
    operator's toggle would appear saved while doing nothing. That is the
    Approval_Config casing gap, one attribute over, and importing the names from
    ``shared/quality_verdict.py`` is the mechanism (``LGR-eee8ac8-5``).

    The technique is this repo's existing one for a cross-boundary vocabulary — see
    ``tests/contracts/test_approval_config_casing_cross_language.py``, which relies
    on the same convention that this module's prose spells these names in double
    backticks and never in double quotes.

    **Validates: Requirements 7.1**
    """

    @pytest.mark.parametrize(
        "name",
        [QUALITY_CONFIG_KEY, DATA_QUALITY_ENABLED_KEY, DEDUP_ENABLED_KEY, DEDUP_COLUMNS_KEY],
    )
    def test_the_writer_declares_no_literal_of_its_own(self, name):
        with open(os.path.abspath(_PIPELINES_CRUD_SOURCE), encoding="utf-8") as handle:
            source = handle.read()

        assert f'"{name}"' not in source, (
            f"{name!r} appears as a string literal in pipelines_crud.py. It is a "
            f"cross-Lambda attribute name with ONE definition in "
            f"shared/quality_verdict.py — import it. A second spelling here does not "
            f"fail: it makes the opt-in silently never match the reader in "
            f"create_code, which is the Approval_Config casing gap again."
        )

    @pytest.mark.parametrize(
        "name",
        [QUALITY_CONFIG_KEY, DATA_QUALITY_ENABLED_KEY, DEDUP_ENABLED_KEY, DEDUP_COLUMNS_KEY],
    )
    def test_every_key_is_snake_case(self, name):
        """The wire convention, stated as a fact about the shared definition.

        p63 R1.2 requires exactly **one** normalisation authority and p64 2a.1b
        placed it at the wizard's request boundary, emitting snake_case. The
        Quality_Config follows the same convention, so p64 2c.2's wizard serialiser
        emits these names and nothing translates on the server. **Do not add a
        server-side camelCase normaliser** — that is the second authority R1.2
        forbids.
        """
        assert name == name.lower()
        assert not any(character.isupper() for character in name)
