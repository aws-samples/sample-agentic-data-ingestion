# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Prompt template loader for AgenticDataIngestion Lambda functions.

Loads Jinja2 templates from the packaged prompt_templates directory — the
repo's single versioned source, bundled into the Lambda layer
(/opt/python/prompt_templates) and vendored shared/ in containers. Templates
use {{ variable }} syntax to avoid conflicts with $ characters in code
examples within prompts.

P28 R19.8: the former S3 fallback loader is deleted. A stale S3 object could
silently diverge from the versioned source; now a missing template fails
loudly (TemplateNotFound) instead of loading unreviewed content. For local
experimentation only, PROMPT_TEMPLATES_DIR may point at an alternate
directory — it takes precedence over the packaged sources and is logged at
WARNING on every environment initialization so an experiment can never run
silently.
"""

import logging
import os

logger = logging.getLogger(__name__)

_jinja_env = None


def _get_jinja_env():
    """Lazily initialize and return the Jinja2 Environment."""
    global _jinja_env
    if _jinja_env is not None:
        return _jinja_env

    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    # Search paths: Lambda layer first, then local shared directory — both are
    # builds of the same versioned shared/prompt_templates source.
    search_paths = [
        os.path.join("/opt/python", "prompt_templates"),
        os.path.join(os.path.dirname(__file__), "prompt_templates"),
    ]

    override_dir = os.environ.get("PROMPT_TEMPLATES_DIR", "")
    if override_dir:
        # Experimentation escape hatch (P28 R19.8): loud by design so an
        # override can never masquerade as the versioned source.
        logger.warning(
            "PROMPT_TEMPLATES_DIR override active — loading prompt templates from %s "
            "INSTEAD of the packaged versioned source",
            override_dir,
        )
        search_paths.insert(0, override_dir)

    # autoescape stays at the jinja2 default: these are plain-text LLM prompts,
    # not HTML (matches every other prompt/pyspark template Environment in this
    # repo; see also the pyspark template render sites).
    _jinja_env = Environment(
        loader=FileSystemLoader(search_paths),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    return _jinja_env


def load_prompt_template(template_name: str, **kwargs) -> str:
    """Load and render a Jinja2 prompt template.

    Args:
        template_name: Template filename (e.g. 'code_generation.j2').
        **kwargs: Variables to substitute into the template.

    Returns:
        The rendered prompt string.

    Raises:
        jinja2.TemplateNotFound: If the template file does not exist in the
            packaged source (there is no S3 fallback — P28 R19.8).
        jinja2.UndefinedError: If a required variable is missing.
    """
    env = _get_jinja_env()
    template = env.get_template(template_name)
    rendered = template.render(**kwargs)
    logger.debug("Rendered prompt template '%s' (%d chars)", template_name, len(rendered))
    return rendered
