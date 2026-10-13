# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit + property tests for P19 ``create_glue_pipeline`` write_mode handling.

Covers the codegen-Lambda half of Decision 2:

    * The default at line ~726 of
      ``lambda/agentic_data_ingestion_create_glue_pipeline/lambda_function.py``
      changed from ``'append'`` to ``'overwrite'`` (P19 Task 5.1).
    * Property 11: the resolved ``write_mode`` flows through
      ``build_jdbc_arguments`` into the Glue job's
      ``DefaultArguments['--write_mode']`` and into the rendered Jinja
      context unchanged (P19 Task 5.2).
    * Req 9.1: a persisted item with no ``write_mode`` field is treated as
      ``'overwrite'`` for backward compatibility with pre-P19 P17 pipelines.

Validates: P19 Requirements 7.4, 7.6, 9.1
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

import jinja2
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Module loading — mock heavy deps so we can import the lambda function.
# Mirrors the bootstrap in test_worker_sizing.py.
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "pyspark_templates")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("MCP_SIGV4_ENABLED", "false")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

_mods = {
    "strands": mock.MagicMock(),
    "strands.agent": mock.MagicMock(),
    "strands.tools.mcp": mock.MagicMock(),
    "streamable_http_sigv4": mock.MagicMock(),
    "mcp": mock.MagicMock(),
    "mcp.client": mock.MagicMock(),
    "mcp.client.streamable_http": mock.MagicMock(),
    "schema_evolution": mock.MagicMock(),
    "metrics_facade": mock.MagicMock(),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock prompt"),
    ),
}


def _load_module():
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_glue_pipeline_lf",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


_mod = _load_module()
build_jdbc_arguments = _mod.build_jdbc_arguments


# ---------------------------------------------------------------------------
# Example tests — Req 7.4, 7.6, 9.1
# ---------------------------------------------------------------------------


_BASE_JDBC_TARGET_CONFIG = {
    "type": "JDBC",
    "jdbc_connection_name": "my-conn",
    "database_name": "analytics",
    "table_name": "users_mirror",
    "jdbc_url": "jdbc:postgresql://db:5432/orders",
    "jdbc_driver": "org.postgresql.Driver",
    # A Secrets Manager reference, not the inline `jdbc_user` / `jdbc_password`
    # pair this fixture used to carry: `build_jdbc_arguments` now ignores that pair
    # (standing invariant `LGR-819980f-1`) and, with no reference on the config,
    # reads the connection's SECRET_ID through GetConnection — a real AWS call in a
    # unit test. The write_mode property is unaffected by which credential is used.
    "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:p19-target-AbCdEf",
}


class TestPersistedItemDefaultsToOverwrite:
    """Req 9.1: legacy P17 items with no write_mode → 'overwrite'.

    A persisted ``agentic_data_ingestion_pipelines`` item with ``target_config.type ==
    'JDBC'`` and no ``write_mode`` field MUST render with ``'overwrite'`` so
    pre-P19 pipelines are not silently rewritten.
    """

    def test_legacy_item_no_write_mode_defaults_to_overwrite(self):
        """The line ~726 default (now 'overwrite' per Task 5.1) controls."""
        target_config = dict(_BASE_JDBC_TARGET_CONFIG)
        # No `write_mode` key — simulating a pre-P19 pipeline.
        assert "write_mode" not in target_config

        args = build_jdbc_arguments(
            target_config=target_config,
            source_path="s3://example/path",
            key_prefix="",
        )
        assert args["--write-mode"] == "overwrite", (
            "Pre-P19 JDBC item with no write_mode field must default to 'overwrite' (Req 9.1, Task 5.1)"
        )


# Feature: p19-jdbc-target-modes, Property 11: Codegen Lambda render context carries write_mode unchanged
class TestProperty11RenderContextCarriesWriteMode:
    """Property 11: codegen Lambda render context carries write_mode unchanged.

    *For any* persisted ``agentic_data_ingestion_pipelines`` item with
    ``target_config.type == 'JDBC'`` and ``target_config.write_mode == w``
    for some valid ``w``, when the JDBC template renders for that item,
    the Jinja2 render context's ``write_mode`` variable equals ``w``
    exactly.

    The codegen Lambda doesn't render templates directly (that's
    ``create_code``'s job — see ``lambda/agentic_data_ingestion_create_code/
    lambda_function.py:249``), but it builds the ``DefaultArguments``
    that feed the Glue job's ``getResolvedOptions`` call. The
    ``--write_mode`` arg in those DefaultArguments is the value the
    rendered code reads at runtime via ``args['write_mode']``. The
    property below pins:

        target_config.write_mode = w
            ⇒ DefaultArguments['--write-mode'] = w
            ⇒ rendered _write_jdbc.py.j2 produces mode="<w>" for w='overwrite',
              else mode="append" (per the template's fallthrough — Property 12)

    The byte-equality between the input ``w`` and the codegen's emitted
    ``--write-mode`` argument is what Property 11 actually pins; the
    template-side biconditional is owned by Property 12 and is asserted
    here as a consistency check.

    Validates: P19 Requirements 7.4
    """

    @given(w=st.sampled_from(["overwrite", "append", "merge"]))
    @settings(max_examples=100)
    def test_write_mode_passed_through_build_jdbc_arguments(self, w: str):
        """The codegen builder writes target_config.write_mode verbatim."""
        target_config = dict(_BASE_JDBC_TARGET_CONFIG)
        target_config["write_mode"] = w

        args = build_jdbc_arguments(
            target_config=target_config,
            source_path="s3://example/path",
            key_prefix="",
        )
        assert args["--write-mode"] == w, (
            f"build_jdbc_arguments must emit --write-mode={w!r} verbatim; got {args['--write-mode']!r}"
        )

    @given(w=st.sampled_from(["overwrite", "append", "merge"]))
    @settings(max_examples=100)
    def test_write_mode_flows_through_to_template_render(self, w: str):
        """End-to-end: target_config.write_mode → render context → mode literal.

        Locks the contract that what the codegen builder emits as
        ``--write-mode`` is exactly what the create_code Lambda passes to
        the Jinja env as ``write_mode``, which the template uses to
        choose its mode literal.
        """
        target_config = dict(_BASE_JDBC_TARGET_CONFIG)
        target_config["write_mode"] = w

        # Step 1: the codegen builder emits the value verbatim.
        args = build_jdbc_arguments(
            target_config=target_config,
            source_path="s3://example/path",
            key_prefix="",
        )
        propagated_write_mode = args["--write-mode"]
        assert propagated_write_mode == w

        # Step 2: render the partial with the propagated value as the
        # template's `write_mode` variable. Property 12 owns the
        # biconditional between `write_mode == 'overwrite'` and
        # `mode="overwrite"`; Property 11 just confirms the value
        # propagates byte-for-byte through the layer boundary.
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
        )
        rendered = env.get_template("_partials/_write_jdbc.py.j2").render(write_mode=propagated_write_mode)
        if w == "overwrite":
            assert 'mode="overwrite"' in rendered
        else:
            # The partial itself still renders 'merge' as append; this pins the
            # partial in isolation, not what a pipeline can reach. Since
            # 2026-10-10 a JDBC 'merge' never gets here: create, edit and
            # `code_assembly.generate_code` refuse it (`shared/write_modes.py`),
            # which replaced P19 Decision 4's silent append fall-through as the
            # runtime safety net.
            assert 'mode="append"' in rendered
