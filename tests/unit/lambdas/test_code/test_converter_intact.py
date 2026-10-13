# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pandas proxy's converter is intact and still live on the default path.

Spec ``real-spark-verification`` task 11.5.
**Validates: Requirement 3.3.**

This whole feature adds a second executor that bypasses ``PySpark2PandasConverter``
entirely. R3.3 says the converter "SHALL remain available and unmodified, so that
the Verification_Executor can select it", and design §3 do-NOT 1 says retirement is
a **later** spec, gated on the shadow-mode evidence this spec produces. This file is
the fence that makes an accidental retirement — or an accidental partial breakage —
fail a test rather than surface as a broken fallback in production.

Why the fence is a test rather than a comment
---------------------------------------------

``LGR-5906ec6-2`` (**template / ratified**) is the house pattern: a deliberate hold
is encoded as a test assertion, because "nothing in the code distinguishes the two"
— a hold and an oversight look identical on disk. Its own words: "The assertion is
the mechanism — turning it on means deleting a test deliberately." Retiring the
converter must mean deleting this file on purpose.

``LGR-4dd9b42-1`` (**template / ratified**) is what makes the fence necessary *now*.
It is this spec's authorisation, and its revisit condition reads: "or PySpark becomes
available to the test environment, at which point the refused shapes can be compared
against real Spark and converted on evidence instead of refused on caution." That
condition is about *converting on evidence*, and the evidence does not exist yet —
shadow mode (task 10.x) is what produces it. So the authorisation to build the second
executor is **not** an authorisation to retire the first one, and the gap between
those two readings is exactly what this file holds open.

``LGR-f6519e1-1``, ``LGR-4099d91-1`` and ``LGR-4099d91-2`` are the three other
records the retirement would retire together (design §3 do-NOT 1). ``LGR-4099d91-2``'s
own revisit condition already contemplates retiring ``LGR-f6519e1-1``'s one-site
bound, so the ordering between them matters and none of it can be done piecemeal.

What is asserted, and what is deliberately NOT
----------------------------------------------

**No count is pinned.** The task text quotes "2,056 lines referenced by 36 test
files / 774 test functions". Measured at HEAD on this task instead: **2,056 lines**
(unchanged), **42** referencing test files, **29,049** lines of test source, **826**
test functions — and **43 / 837** once this file is itself counted. The population
grew while the spec was being written and grew again by adding the fence, which is
the whole argument against an equality assertion: a test that fails when someone
*adds* a converter test is measuring test-suite churn, not converter retirement. The
size assertions below are therefore **floors with deliberate slack**: deletion and
gutting trip them, addition and ordinary refactoring do not.

The four things that would actually constitute a regression here:

1. **The module or one of its public names is gone.** ``code_interpreter_runner.py:39-42``
   does ``from pyspark_to_pandas import REFUSAL_REASON_SCALAR_LITERAL, ConversionError,
   PySpark2PandasConverter`` at *module scope*, so losing any one of the three is not a
   degraded fallback — it is an ``ImportError`` at Lambda cold start on the default path.
2. **It no longer converts.** An import-only assertion cannot see this. A module can
   be present, importable, and gutted.
3. **It no longer refuses.** The more dangerous half: if ``ConversionError`` stopped
   being raised, an unconvertible transform would be silently mistranslated and the
   fallback would report a verification it did not perform (P28 R10.2).
4. **It is present but orphaned.** Someone leaves the file on disk and stops calling
   it. R3.3's "so that the Verification_Executor can select it" is a *reachability*
   claim, not a presence claim, so the live edge is asserted directly.

Sibling coverage — not duplicated here
--------------------------------------

* ``test_run_test_composition.py::test_the_module_imports_no_converter`` asserts the
  **mirror** of item 4: the Glue runner imports no converter (task 8.1). This file
  asserts the pandas runner still does. Together they pin one edge present and one
  absent; neither alone says which executor owns the proxy.
* ``test_pyspark_to_pandas.py`` and 41 sibling files are the converter's own suite —
  its translation rules, its refusal vocabulary and its literal fidelity. This file
  asserts that suite still *exists* and that the module still round-trips; it does
  not re-test a single rule.
* ``test_executor_selection_totality_prop.py`` (Property 1, task 9.2) and
  ``test_verification_executor_selection.py`` (task 9.1) own executor resolution.
  Nothing here asserts how a configuration string resolves.
"""

import ast
import inspect
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import code_interpreter_runner
import pyspark_to_pandas
from code_interpreter_runner import CodeInterpreterTestRunner
from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

#: ``agentic_data_ingestion/`` — parents[4] of ``tests/unit/lambdas/test_code/<this>``.
_PACKAGE_ROOT = Path(__file__).resolve().parents[4]
_CONVERTER = _PACKAGE_ROOT / "lambda" / "agentic_data_ingestion_test_code" / "pyspark_to_pandas.py"
_TESTS_ROOT = _PACKAGE_ROOT / "tests"

#: Exactly the names ``code_interpreter_runner.py:39-42`` imports at module scope.
#: Enumerated rather than derived from ``dir()`` so that the assertion fails when a
#: name the DEFAULT PATH needs disappears, not merely when the module's surface moves.
_RUNNER_IMPORTS = ("REFUSAL_REASON_SCALAR_LITERAL", "ConversionError", "PySpark2PandasConverter")

#: Both halves are already pinned by the converter's own suite — the ``withColumn``
#: form at ``test_pyspark_to_pandas.py:437`` and the nested ``trim(lower(col(...)))``
#: composition at ``:178`` — rather than invented here, so this file cannot drift
#: into asserting a rule the converter does not claim. Nested string composition is
#: also the bug class ``LGR-5c33c04-1`` closed (since superseded by
#: ``LGR-4dd9b42-1``), so a regression here is a regression to a known defect.
CONVERTIBLE = 'df_transformed = df.withColumn("name", trim(lower(col("name"))))'

#: ``initcap`` is a ``_UNSUPPORTED_FUNCTIONS`` member (``pyspark_to_pandas.py:573-582``)
#: and is the construct design C10 measured EXECUTING SUCCESSFULLY on the probe
#: session. It is the single clearest case of "real Spark dissolves the refusal" —
#: and precisely because of that, it is the case where the refusal must keep working
#: until the Glue executor is the one actually running. Same transform as
#: ``test_run_test_composition.py::TRANSFORM``, on purpose: one construct, two
#: executors, opposite expected outcomes.
REFUSED = 'df_transformed = df.withColumn("name", initcap(col("name")))'

#: Size floors. Measured at HEAD: 2,056 module lines, 42 referencing test files,
#: 826 test functions. The floors sit well below every measured value so that a
#: refactor, a file merge or the removal of one obsolete test does not trip them,
#: and far enough above zero that deletion or gutting cannot slip through. They are
#: a smoke alarm for do-NOT 1, not a pin on the module's shape.
_MIN_CONVERTER_LINES = 1500
_MIN_REFERENCING_TEST_FILES = 30
_MIN_REFERENCING_TEST_FUNCTIONS = 600


def _referencing_test_files() -> list[Path]:
    """Every ``test_*.py`` under ``tests/`` whose source names the converter.

    Recomputed on each run rather than listed, so adding a converter test cannot
    fail this file and deleting the suite cannot pass it.
    """
    found = []
    for path in sorted(_TESTS_ROOT.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if any(name in source for name in ("pyspark_to_pandas", *_RUNNER_IMPORTS[1:])):
            found.append(path)
    return found


def _count_test_functions(paths: list[Path]) -> int:
    """``def test_*`` / ``async def test_*`` at any nesting depth, counted by AST.

    AST rather than a line grep: the converter's suite uses test classes, and a
    grep anchored to column zero would undercount every method inside one.
    """
    total = 0
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
                total += 1
    return total


@pytest.fixture
def proxy_runner():
    """A ``CodeInterpreterTestRunner`` whose client is a stub.

    The client only has to be non-``None``: ``run_test`` checks it and returns early
    when it is missing (``code_interpreter_runner.py:268-270``) before reaching the
    converter at ``:273``, and both drives below refuse BEFORE ``_start_session``, so
    no interpreter call is ever made.
    **No AWS call happens anywhere in this file.**
    """
    with patch("code_interpreter_runner.boto3.client", return_value=MagicMock()):
        yield CodeInterpreterTestRunner(region="us-east-1")


# ---------------------------------------------------------------------------
# 1. The module is present, and every name the default path imports exists
# ---------------------------------------------------------------------------


def test_the_converter_module_is_present_on_disk():
    """do-NOT 1's literal reading: the file has not been deleted."""
    assert _CONVERTER.is_file(), f"the pandas proxy's converter is missing from {_CONVERTER}"


@pytest.mark.parametrize("name", _RUNNER_IMPORTS)
def test_a_name_the_default_path_imports_still_exists(name: str):
    """Losing one of these is an ImportError at cold start, not a soft degradation.

    ``code_interpreter_runner.py`` imports all three at module scope, so the failure
    would take down the pandas executor — the fallback — in its entirety.
    """
    assert hasattr(pyspark_to_pandas, name), (
        f"`{name}` is imported at module scope by code_interpreter_runner.py:39-42; "
        "removing it breaks the pandas proxy at import time"
    )


def test_the_converter_class_constructs():
    """R3.3's "remain available", at the class."""
    assert isinstance(PySpark2PandasConverter(), PySpark2PandasConverter)


def test_convert_is_still_a_classmethod_over_one_argument():
    """The runner calls it on the CLASS (``code_interpreter_runner.py:273``).

    Asserted as a classmethod rather than just callable because an instance method
    would still pass a ``hasattr`` check and then fail at the one call site that
    matters.
    """
    assert isinstance(inspect.getattr_static(PySpark2PandasConverter, "convert"), classmethod)
    parameters = list(inspect.signature(PySpark2PandasConverter.convert).parameters)
    assert parameters == ["pyspark_code"]


# ---------------------------------------------------------------------------
# 2. It still converts, and it still refuses
# ---------------------------------------------------------------------------


def test_a_convertible_transform_still_converts():
    """Presence is not function: this is the assertion an import test cannot make."""
    result = PySpark2PandasConverter.convert(CONVERTIBLE)

    assert sorted(result) == ["code", "conversion_report"]
    assert result["code"].startswith("import pandas as pd\nimport numpy as np\n")
    assert sorted(result["conversion_report"]) == ["constructs_converted", "input_lines", "output_lines"]
    # The rules themselves belong to the converter's own suite; what is asserted
    # here is only that real translation happened — a stub returning the input
    # would report no constructs.
    assert result["conversion_report"]["constructs_converted"]
    assert result["conversion_report"]["output_lines"] >= 1


def test_a_refused_construct_still_raises_conversion_error():
    """The load-bearing half of do-NOT 1.

    A converter that stopped refusing would not look broken — it would look like a
    converter with better coverage, while silently mistranslating transforms and
    reporting verifications it never performed (P28 R10.2).
    """
    with pytest.raises(ConversionError) as raised:
        PySpark2PandasConverter.convert(REFUSED)

    assert raised.value.construct == "initcap"
    assert raised.value.line_number == 1
    assert raised.value.line == REFUSED


# ---------------------------------------------------------------------------
# 3. It is still LIVE on the default path, not merely present
# ---------------------------------------------------------------------------


def test_the_pandas_runner_holds_a_live_edge_to_the_converter():
    """R3.3 is a reachability claim. An orphaned module satisfies presence, not this.

    Identity rather than name equality, so a local re-definition or a shim that
    shadows the import cannot pass.
    """
    assert code_interpreter_runner.PySpark2PandasConverter is PySpark2PandasConverter
    assert code_interpreter_runner.ConversionError is ConversionError
    assert code_interpreter_runner.NOT_VERIFIED_REASON_SCALAR_LITERAL == (
        pyspark_to_pandas.REFUSAL_REASON_SCALAR_LITERAL
    )


def test_run_test_still_routes_a_refusal_through_the_converter(proxy_runner):
    """The proxy non-verification is only reachable if the converter really ran.

    This is the marker the Glue executor must never produce (R3.2) and the pandas
    executor must still be able to produce — the two halves of the same fact.
    """
    results = proxy_runner.run_test(REFUSED, {"properties": {"name": {"type": "string"}}}, {})

    assert results["not_verified_by_proxy"] is True
    assert results["not_verified_construct"] == "initcap"
    assert results["execution_successful"] is False
    assert results["executed"] is False
    # No session was spent on a refusal (P28 R10.2), so the stub client was never
    # asked to do anything.
    assert proxy_runner.session_id is None


def test_run_test_still_carries_a_real_conversion_report(proxy_runner):
    """The success direction of the same edge, still without a session.

    An empty source schema refuses at ``_frame_refusal``
    (``code_interpreter_runner.py:339-345``) AFTER conversion has already run and
    succeeded, so the report on the result is the real converter's output. That
    ordering is what lets this assertion exist at all.
    """
    results = proxy_runner.run_test(CONVERTIBLE, {}, {})

    assert results["not_verified_by_harness"] is True
    assert results["conversion_report"] is not None
    assert results["conversion_report"]["constructs_converted"]
    assert proxy_runner.session_id is None


# ---------------------------------------------------------------------------
# 4. Neither the module nor its suite has been retired — floors, never pins
# ---------------------------------------------------------------------------


def test_the_converter_module_has_not_been_gutted():
    """do-NOT 1 forbids "delete or shrink", and shrink needs its own assertion."""
    lines = len(_CONVERTER.read_text(encoding="utf-8").splitlines())
    assert lines >= _MIN_CONVERTER_LINES, (
        f"pyspark_to_pandas.py is {lines} lines, below the {_MIN_CONVERTER_LINES}-line floor "
        "(2,056 at the time this fence was written). design §3 do-NOT 1: retirement is a "
        "later spec gated on shadow-mode evidence"
    )


def test_the_converter_suite_has_not_been_retired():
    """The suite is the converter's real specification, so it is fenced too.

    A floor on both files and functions: the two fail differently. Deleting whole
    files trips the first; hollowing files out while keeping them trips the second.
    """
    files = _referencing_test_files()
    functions = _count_test_functions(files)

    assert len(files) >= _MIN_REFERENCING_TEST_FILES, (
        f"{len(files)} test files reference the converter, below the "
        f"{_MIN_REFERENCING_TEST_FILES}-file floor (42 when this fence was written)"
    )
    assert functions >= _MIN_REFERENCING_TEST_FUNCTIONS, (
        f"{functions} test functions live in those files, below the "
        f"{_MIN_REFERENCING_TEST_FUNCTIONS}-function floor (826 when this fence was written)"
    )
