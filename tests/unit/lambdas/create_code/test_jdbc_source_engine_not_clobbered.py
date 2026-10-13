# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression: the SOURCE engine must survive the source/target context merge.

`code_assembly.generate_code` builds the Jinja2 context by merging both configs
into one flat namespace::

    template_vars.update(source_config)
    template_vars.update(target_config)   # target wins every collision

`engine` exists in BOTH a JDBC `source_config` and a JDBC `target_config`, so the
target's engine silently replaced the source's. `glue_job_jdbc_source.py.j2` bakes
`{{ engine }}` into `quote_identifier(...)` at render time for the SOURCE read, so
a MySQL source feeding an Aurora-PostgreSQL target rendered::

    _sch = quote_identifier("aurora-postgresql", args['jdbc_schema'])

which produces `SELECT * FROM "appdb"."customers" WHERE 1=0`. MySQL rejects
double-quoted identifiers unless `ANSI_QUOTES` is set, so the Glue job failed with
"You have an error in your SQL syntax" — after every prior step, including
`create_glue_pipeline`, had reported SUCCEEDED. Observed on
`run-2026-09-22-20-35-04-689a0a7b`.

**Why nothing caught it.** The engines have to DIFFER for the bug to be visible,
and nothing in the suite had a pipeline where they did:

  * The golden-script tests (`tests/unit/templates/test_golden_script_snapshot.py`)
    render the template from a FLAT config dict, so they never execute the merge
    where the bug lives. Their goldens correctly bake the source engine and always
    would.
  * `test_jdbc_source_target_type_injection.py` does go through `generate_code`,
    but its fixture's `target_config` carries no `engine` key at all
    (`:87-100`) — so no collision occurs and `engine` stays the source's. Its own
    comment correctly names `engine` as the only render-time var read from
    `source_config`; the fixture just never contested it.

These tests contest it. The fixture below is the shape of a real persisted
JDBC→JDBC pipeline: both configs carry `engine`, and they differ.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_create_code_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_create_code",
    )
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "parameter_extractor": MagicMock(),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _mixed_engine_metadata(source_engine: str, target_engine: str) -> dict:
    """A JDBC→JDBC pipeline whose two configs BOTH carry `engine`, differing.

    Modelled on the persisted record for `mynew233-503c3151`: a MySQL source
    into an Aurora-PostgreSQL target.
    """
    return {
        "source_type": "JDBC",
        "source_config": {
            "engine": source_engine,
            "read_mode": "full_table",
            "jdbc_url": "jdbc:mysql://src.example:3306/appdb",
            "jdbc_driver": "com.mysql.cj.jdbc.Driver",
            "jdbc_schema": "appdb",
            "jdbc_table": "customers",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:src-AbCdEf",
        },
        "target_config": {
            # The half the old fixture omitted. A real JDBC target always
            # carries `engine` — the wizard writes it from the RDS selection.
            "engine": target_engine,
            "type": "jdbc",
            "jdbc_url": "jdbc:postgresql://tgt.example:5432/testdb",
            "jdbc_driver": "org.postgresql.Driver",
            "jdbc_table": "public.customers",
            "database_name": "public",
            "table_name": "customers",
            "write_mode": "append",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:tgt-AbCdEf",
        },
        "source_schema": {"properties": {}},
    }


def _render(metadata: dict) -> str:
    with patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"):
        return _mod.generate_code(pipeline_metadata=metadata, mapping_result={"field_mappings": []})


class TestSourceEngineSurvivesTheMerge:
    def test_mysql_source_into_postgres_target_quotes_with_the_source_engine(self):
        """The exact production shape that failed."""
        code = _render(_mixed_engine_metadata("mysql", "aurora-postgresql"))

        assert 'quote_identifier("mysql"' in code, (
            "the source read must be quoted for the SOURCE engine; found "
            f"{[ln for ln in code.splitlines() if 'quote_identifier(' in ln]}"
        )
        assert 'quote_identifier("aurora-postgresql"' not in code, (
            "the TARGET engine leaked into the source read's identifier quoting — "
            "this is the defect: MySQL rejects the double quotes it produces"
        )

    @pytest.mark.parametrize(
        "source_engine,target_engine",
        [
            ("mysql", "aurora-postgresql"),
            ("mariadb", "postgres"),
            ("aurora-mysql", "sqlserver-ee"),
            # The reverse direction matters just as much: a Postgres source into
            # a MySQL target must NOT pick up the target's backticks.
            ("postgres", "mysql"),
            ("oracle-ee", "aurora-mysql"),
        ],
    )
    def test_source_engine_wins_in_both_directions(self, source_engine: str, target_engine: str):
        code = _render(_mixed_engine_metadata(source_engine, target_engine))

        quoting_lines = [ln for ln in code.splitlines() if "quote_identifier(" in ln and "def " not in ln]
        assert quoting_lines, "template emitted no quote_identifier call"
        for line in quoting_lines:
            assert f'"{source_engine}"' in line, f"expected source engine {source_engine!r} in: {line.strip()}"
            assert f'"{target_engine}"' not in line, f"target engine {target_engine!r} leaked into: {line.strip()}"

    def test_same_engine_pipelines_are_unaffected(self):
        """The case that always worked keeps working — and is why this hid.

        With both engines equal the merge order is unobservable, so a
        same-engine pipeline never showed the defect.
        """
        code = _render(_mixed_engine_metadata("postgres", "postgres"))
        assert 'quote_identifier("postgres"' in code

    def test_a_target_engine_alone_does_not_bind_engine(self):
        """A non-JDBC source leaves `engine` to the target, which has no reader.

        The re-assert is conditional on `source_config.get("engine")`, so this
        documents that the conditional does not resurrect the bug: with no source
        engine there is no source-side JDBC read to quote in the first place.
        """
        meta = _mixed_engine_metadata("mysql", "aurora-postgresql")
        del meta["source_config"]["engine"]
        # Must not raise — StrictUndefined would fire if `engine` were unbound.
        code = _render(meta)
        assert "quote_identifier(" in code


class TestTheInitialWatermarkReachesTheTemplate:
    """The second render-time key the merge did not bind (2026-10-10).

    The template reads `initial_watermark`; the wizard stores the seed as
    `watermark_initial_value`. The flat merge copied the stored key under its
    own name, so the template's variable stayed `''` and every first
    incremental run read the whole table, whatever seed the user entered.
    Expectations come from the requirement — the seed the user entered is the
    first run's lower bound — not from the current output.
    """

    @staticmethod
    def _incremental(**seed: str) -> dict:
        meta = _mixed_engine_metadata("postgres", "postgres")
        meta["source_config"].update(read_mode="incremental", watermark_column="id", watermark_type="numeric", **seed)
        return meta

    def test_the_wizard_seed_is_the_first_run_bound(self):
        code = _render(self._incremental(watermark_initial_value="100"))
        assert 'prev_value = "100"' in code

    def test_the_documented_legacy_key_still_reaches_the_template(self):
        """API callers were told to send `initial_watermark`; their records keep working."""
        code = _render(self._incremental(initial_watermark="100"))
        assert 'prev_value = "100"' in code

    def test_the_wizard_key_wins_over_a_legacy_key(self):
        code = _render(self._incremental(watermark_initial_value="200", initial_watermark="100"))
        assert 'prev_value = "200"' in code

    def test_no_seed_renders_an_empty_bound(self):
        code = _render(self._incremental())
        assert 'prev_value = ""' in code


class TestAStoredWriteModeTheTargetCannotRunIsRefused:
    """Code generation refuses a write mode the target's writer does not implement (2026-10-10).

    Create and edit now refuse these at the API; this covers a record stored
    before they did. The JDBC writer used to render a stored `merge` as append,
    which the run then reported as success.
    """

    # Matched by name: `patch.dict` removes the modules loaded while this file
    # imported `lambda_function`, so a fresh `from step_errors import StepFailure`
    # here is a different class object from the one code_assembly raises.

    def test_a_stored_jdbc_merge_fails_code_generation_with_a_reason(self):
        meta = _mixed_engine_metadata("postgres", "postgres")
        meta["target_config"].update(write_mode="merge", merge_key="id")
        with pytest.raises(Exception) as raised:
            _render(meta)
        assert type(raised.value).__name__ == "StepFailure"
        message = raised.value.message
        assert "merge" in message and "S3 Tables" in message
        assert "append" in message and "overwrite" in message
        assert raised.value.error_type == "permanent"

    def test_the_refusal_comes_before_the_ai_call(self):
        meta = _mixed_engine_metadata("postgres", "postgres")
        meta["target_config"]["write_mode"] = "merge"
        with (
            patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df") as ai,
            pytest.raises(Exception) as raised,
        ):
            _mod.generate_code(pipeline_metadata=meta, mapping_result={"field_mappings": []})
        assert type(raised.value).__name__ == "StepFailure"
        ai.assert_not_called()

    @pytest.mark.parametrize("mode", ["append", "overwrite"])
    def test_supported_jdbc_modes_still_render(self, mode: str):
        meta = _mixed_engine_metadata("postgres", "postgres")
        meta["target_config"]["write_mode"] = mode
        assert f'mode="{mode}"' in _render(meta)
