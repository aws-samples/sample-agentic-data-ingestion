# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/prompt_template_loader.py (P28 R19.8).

Prompts have ONE versioned source (shared/prompt_templates, bundled into the
Lambda layer and the vendored container shared/). The former S3 fallback
loader is deleted — a stale S3 object can never silently diverge from the
versioned source; a missing template now fails loudly with TemplateNotFound.
The PROMPT_TEMPLATES_DIR experimentation override is retained but logged at
WARNING on environment initialization so it can never run silently.
"""

import importlib
import logging
import os
import sys

import pytest

SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

import prompt_template_loader  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_module(monkeypatch):
    """Reset the module-level Jinja env cache around every test."""
    monkeypatch.delenv("PROMPT_TEMPLATES_DIR", raising=False)
    importlib.reload(prompt_template_loader)
    yield
    importlib.reload(prompt_template_loader)


class TestPackagedSourceOnly:
    def test_loads_template_from_packaged_source(self):
        rendered = prompt_template_loader.load_prompt_template(
            "code_generation.j2",
            **_render_vars_for("code_generation.j2"),
        )
        assert isinstance(rendered, str)
        assert rendered.strip()

    def test_missing_template_raises_template_not_found(self):
        from jinja2 import TemplateNotFound

        with pytest.raises(TemplateNotFound):
            prompt_template_loader.load_prompt_template("definitely_not_a_template.j2")

    def test_no_s3_fallback_in_module(self):
        """R19.8 guard: the loader must not touch S3/boto3 at all."""
        source = open(prompt_template_loader.__file__).read()
        assert "boto3" not in source, "S3 fallback (boto3) reintroduced into the prompt loader"
        assert "TEMPLATES_BUCKET" not in source, "prompt loader reads the S3 templates bucket again"
        assert "get_object" not in source

    def test_templates_bucket_env_is_ignored(self, monkeypatch):
        """A configured bucket must not change behavior — missing is missing."""
        from jinja2 import TemplateNotFound

        monkeypatch.setenv("TEMPLATES_BUCKET", "some-bucket-that-should-not-be-read")
        with pytest.raises(TemplateNotFound):
            prompt_template_loader.load_prompt_template("definitely_not_a_template.j2")


class TestExperimentationOverride:
    def test_override_dir_takes_precedence_and_logs_loudly(self, tmp_path, monkeypatch, caplog):
        override = tmp_path / "prompt_templates"
        override.mkdir()
        (override / "override_only.j2").write_text("override says {{ word }}")

        monkeypatch.setenv("PROMPT_TEMPLATES_DIR", str(override))
        importlib.reload(prompt_template_loader)

        with caplog.at_level(logging.WARNING, logger="prompt_template_loader"):
            rendered = prompt_template_loader.load_prompt_template("override_only.j2", word="hello")

        assert rendered == "override says hello"
        assert any(
            "PROMPT_TEMPLATES_DIR" in record.message for record in caplog.records
        ), "override activation was not logged loudly"


def _render_vars_for(template_name: str) -> dict:
    """Extract the undeclared variables of a packaged template and stub them.

    StrictUndefined means every referenced variable must be supplied; the
    test only cares that the packaged source renders, not about content.
    """
    from jinja2 import Environment, meta

    path = os.path.join(SHARED_DIR, "prompt_templates", template_name)
    with open(path) as f:
        source = f.read()
    env = Environment()
    variables = meta.find_undeclared_variables(env.parse(source))
    return dict.fromkeys(variables, "stub")
