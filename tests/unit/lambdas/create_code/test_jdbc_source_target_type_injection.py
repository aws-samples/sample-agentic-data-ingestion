# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression: generate_code must inject an UPPERCASE `target_type` into the
JDBC-source template render context.

Bug: the persisted `target_config` carries the target type under the key
`type` (e.g. "jdbc"), not `target_type`. `generate_code` never copied it into
`template_vars`, so `glue_job_jdbc_source.py.j2` fell through to its
`target_type | default('UNKNOWN')` branch. With target_type == 'UNKNOWN' the
template matched NO write branch — it emitted a job that read + transformed
the source but wrote NOTHING, and declared none of the `target_jdbc_*`
getResolvedOptions keys. The Glue run reported SUCCEEDED while writing zero
rows.

Fix: `generate_code` now sets ``template_vars["target_type"] = target_type.upper()``
so the JDBC-source template renders the JDBC write partial.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import jinja2
import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir and shared dir to path
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
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "pyspark_templates")

# Stub heavy dependencies so the module imports cleanly in unit context.
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


# Unique marker emitted only by _partials/_write_jdbc.py.j2.
_JDBC_WRITE_MARKER = "Write to JDBC target complete"


def _jdbc_pipeline_metadata() -> dict:
    """A JDBC-source → JDBC-target pipeline whose target type is under `type`."""
    return {
        "source_type": "JDBC",
        "source_config": {
            # `engine` is the only render-time var the JDBC-source template
            # reads from source_config ({{ engine }} for identifier quoting).
            "engine": "postgres",
            "jdbc_url": "jdbc:postgresql://src:5432/srcdb",
        },
        "target_config": {
            # The real persisted shape: the type lives under `type`, NOT
            # `target_type`. This is precisely what triggered the bug.
            "type": "jdbc",
            "jdbc_url": "jdbc:postgresql://tgt:5432/testdb",
            "jdbc_table": "public.customers",
            "write_mode": "overwrite",
        },
        "source_schema": {"properties": {}},
    }


def test_generate_code_injects_uppercase_target_type_and_renders_jdbc_write():
    """generate_code renders the JDBC write block for a `type: jdbc` target."""
    with patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"):
        code = _mod.generate_code(
            pipeline_metadata=_jdbc_pipeline_metadata(),
            mapping_result={"field_mappings": []},
        )

    # The write partial must be present — this is the actual zero-rows fix.
    assert _JDBC_WRITE_MARKER in code, (
        "JDBC write block missing — target_type was not injected, so the "
        "template fell through to the UNKNOWN (no-write) branch."
    )
    # The JDBC target getResolvedOptions keys must be declared too.
    assert "target_jdbc_url" in code
    assert "target_jdbc_table" in code


def test_generate_code_target_type_injected_uppercase_in_template_vars():
    """The injected target_type value is the UPPERCASE template literal."""
    captured = {}

    real_render = jinja2.Template.render

    def _capture_render(self, *args, **kwargs):
        captured.update(kwargs)
        return real_render(self, *args, **kwargs)

    with (
        patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"),
        patch.object(jinja2.Template, "render", _capture_render),
    ):
        _mod.generate_code(
            pipeline_metadata=_jdbc_pipeline_metadata(),
            mapping_result={"field_mappings": []},
        )

    assert captured.get("target_type") == "JDBC", f"expected uppercase 'JDBC', got {captured.get('target_type')!r}"


def test_jdbc_source_template_emits_no_write_block_when_target_type_unknown():
    """Documents the bug: with no target_type the template writes nothing.

    This guards the template contract that the fix relies on — if the default
    branch ever started emitting a write block this test would catch the
    silent behaviour change.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    template = env.get_template("glue_job_jdbc_source.py.j2")
    # Render WITHOUT target_type → template default('UNKNOWN').
    rendered = template.render(
        engine="postgres",
        transform_block="df_transformed = df",
    )
    assert _JDBC_WRITE_MARKER not in rendered
    assert "target_jdbc_url" not in rendered


def test_generate_code_secret_backed_target_uses_runtime_fetch_no_plaintext():
    """When target_config has a secret_arn, the generated JDBC target write
    fetches credentials at runtime and does NOT declare plaintext
    target_jdbc_user / target_jdbc_password getResolvedOptions keys.

    Security regression guard: plaintext credentials must never be passed as
    Glue job arguments (visible in console / CloudTrail / run history).
    """
    meta = _jdbc_pipeline_metadata()
    meta["target_config"]["secret_arn"] = (
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/tgt-AbCdEf"
    )

    with patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"):
        code = _mod.generate_code(pipeline_metadata=meta, mapping_result={"field_mappings": []})

    # Runtime secret fetch for the target is present. (Since the JDBC-target
    # credential fix this holds with or without a secret_arn in the render
    # context — see the next test.)
    assert "SecretId=args['target_secret_arn']" in code
    # getResolvedOptions declares the secret ARN, not plaintext creds.
    args_block = code[code.index("getResolvedOptions(sys.argv, [") : code.index("])")]
    assert "'target_secret_arn'" in args_block
    assert "'target_jdbc_password'" not in args_block
    assert "'target_jdbc_user'" not in args_block


def test_generate_code_target_without_secret_arn_still_uses_runtime_fetch():
    """Without a target secret_arn the generated code is IDENTICAL in credential
    handling: it declares `target_secret_arn` and fetches at run time.

    This test used to be `test_generate_code_inline_cred_target_keeps_plaintext_arg_path`
    and asserted the opposite — that a target with no `secret_arn` rendered the
    plaintext `target_jdbc_user` / `target_jdbc_password` getResolvedOptions arm.
    That arm is what leaked a target credential, and it is deleted (standing
    invariant `LGR-819980f-1`). `create_glue_pipeline` now resolves the reference
    itself — `target_config.secret_arn`, else the connection's SECRET_ID — and
    emits `--target_secret_arn` for every JDBC target, so the script must declare
    that key whether or not the render context saw a secret.
    """
    meta = _jdbc_pipeline_metadata()  # no secret_arn

    with patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"):
        code = _mod.generate_code(pipeline_metadata=meta, mapping_result={"field_mappings": []})

    args_block = code[code.index("getResolvedOptions(sys.argv, [") : code.index("])")]
    assert "'target_secret_arn'" in args_block
    assert "'target_jdbc_user'" not in args_block
    assert "'target_jdbc_password'" not in args_block
    assert "SecretId=args['target_secret_arn']" in code
    # No read of a plaintext target credential argument anywhere in the script.
    assert "args['target_jdbc_password']" not in code
    assert "args['target_jdbc_user']" not in code
