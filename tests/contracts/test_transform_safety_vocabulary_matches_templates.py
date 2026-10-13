# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract: the T-01 guard admits exactly the names the job templates bind.

``create_code/transform_safety.TRANSFORM_VOCABULARY`` is a hardcoded copy of
the ``pyspark.sql.functions`` and ``pyspark.sql.types`` names that the five
``pyspark_templates/glue_job_*.py.j2`` scaffolds import at module scope. A
hardcoded copy drifts in two directions, each with its own cost:

* a template gains an import the guard lacks → the guard rejects a block the
  prompt was allowed to produce, and runs fail;
* the guard admits a name no template binds → a block passes the guard and
  fails with ``NameError`` in the job.

So the oracle is the templates' own source, parsed at test time, the same
technique and the same governing record as
``test_transform_namespace_matches_templates.py``: ``LGR-28b4d75-5``
(**template / ratified**), a contract test derives its expectation from the
producer's source because a test written from the producer's output cannot
detect the producer drifting. Equality is asserted because it is achievable:
the guard admits the union of the two template variants
(``spark_max`` is bound only by ``glue_job_jdbc_source.py.j2``), which is
exactly the set of names some template binds.
"""

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES_DIR = _REPO_ROOT / "pyspark_templates"
_CREATE_CODE_DIR = _REPO_ROOT / "lambda" / "agentic_data_ingestion_create_code"

#: Named, not globbed: a glob that matched nothing would make this file vacuous.
_TEMPLATE_NAMES = (
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_jdbc_source.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_s3_tables.py.j2",
)
_VOCABULARY_MODULES = ("pyspark.sql.functions", "pyspark.sql.types")


def _template_vocabulary() -> set[str]:
    """Names bound by column-0 ``from <module> import …`` lines, resolved by ``ast``."""
    names: set[str] = set()
    for template in _TEMPLATE_NAMES:
        path = _TEMPLATES_DIR / template
        assert path.is_file(), f"{template} not found at {path}; _TEMPLATE_NAMES is the list under contract"
        for line in path.read_text(encoding="utf-8").splitlines():
            if any(line.startswith(f"from {module} import ") for module in _VOCABULARY_MODULES):
                for node in ast.walk(ast.parse(line)):
                    if isinstance(node, ast.ImportFrom):
                        names.update(alias.asname or alias.name for alias in node.names)
    return names


def _guard_vocabulary() -> frozenset[str]:
    if str(_CREATE_CODE_DIR) not in sys.path:
        sys.path.insert(0, str(_CREATE_CODE_DIR))
    from transform_safety import TRANSFORM_VOCABULARY

    return TRANSFORM_VOCABULARY


def test_the_template_parse_is_not_vacuous():
    """An empty oracle would be a subset of everything; pin known members."""
    vocabulary = _template_vocabulary()

    assert {"col", "expr", "initcap", "spark_count", "spark_max", "DateType", "IntegerType"} <= vocabulary
    assert "count" not in vocabulary, "the `count as spark_count` alias was resolved in the wrong direction"


def test_every_name_the_templates_bind_is_admitted_by_the_guard():
    missing = sorted(_template_vocabulary() - _guard_vocabulary())

    assert not missing, (
        f"pyspark_templates bind {missing} for the transform block, but transform_safety rejects them. "
        "A block using one fails create_code although the job could run it. Add them to TRANSFORM_VOCABULARY."
    )


def test_the_guard_admits_nothing_the_templates_do_not_bind():
    extra = sorted(_guard_vocabulary() - _template_vocabulary())

    assert not extra, (
        f"transform_safety admits {extra}, which no pyspark_templates template binds at module scope. "
        "A block using one passes the guard and fails with NameError in the Glue job."
    )
