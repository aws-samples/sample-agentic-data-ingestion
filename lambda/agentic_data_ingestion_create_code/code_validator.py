# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import ast
import logging
import re
from typing import Any

logger = logging.getLogger()


def _compute_quality_score(summary: dict, antipattern_count: int = 0) -> int:
    """Compute an integer quality score in [0, 100] from observed summary signals.

    P21 Req 2.4/2.5: the score is derived from syntax validity, presence of imports,
    presence of function/class definitions, placeholder count, and antipattern findings
    rather than a constant. Invalid (non-parseable) code scores 0, strictly below any
    valid sample, which always starts from a positive baseline.
    """
    if not summary.get("syntax_valid", False):
        return 0
    score = 60
    if summary.get("has_imports"):
        score += 10
    if summary.get("has_definitions"):
        score += 15
    score -= 5 * int(summary.get("placeholder_count", 0))
    score -= 10 * antipattern_count
    return max(0, min(100, score))


def validate_generated_code(code: str, *, antipattern_count: int = 0) -> dict:
    """
    Comprehensive validation of generated Python code.

    Args:
        code: The generated Python code to validate
        antipattern_count: Number of PySpark anti-pattern findings detected by the
            caller's Antipattern_Checker (``validate_pyspark_antipatterns``). Passed in
            via dependency injection so this module stays self-contained and avoids a
            circular import with ``lambda_function``. Keyword-only with a default of 0 so
            existing callers that pass only ``code`` are unaffected (P21 Req 8.3). When
            greater than 0, the findings are surfaced as a warning and lower the score
            (P21 Req 2.6).

    Returns:
        dict: Validation result with is_valid, errors, warnings, and summary
    """
    validation_result: dict[str, Any] = {"is_valid": True, "errors": [], "warnings": [], "summary": {}}

    try:
        # Basic checks - only fail on critical issues
        if not code or not code.strip():
            validation_result["errors"].append("Code is empty or whitespace only")
            validation_result["is_valid"] = False
            return validation_result

        if len(code.strip()) < 50:  # Much more permissive
            validation_result["errors"].append(
                f"Code is too short ({len(code.strip())} characters) - likely incomplete"
            )
            validation_result["is_valid"] = False

        # Check for Python syntax validity - this is critical
        try:
            ast.parse(code)
            validation_result["summary"]["syntax_valid"] = True
        except SyntaxError as e:
            validation_result["errors"].append(f"Syntax error: {e!s}")  # includes lineno/offset
            validation_result["is_valid"] = False
            validation_result["summary"]["syntax_valid"] = False
        except Exception as e:  # P21 Req 2: any parse failure invalidates code (was: downgrade to warning)
            validation_result["errors"].append(f"Code could not be parsed: {e!s}")
            validation_result["is_valid"] = False
            validation_result["summary"]["syntax_valid"] = False

        # Check for basic Python structure - be more permissive
        has_imports = "import " in code or "from " in code
        has_functions = "def " in code or "class " in code

        # Store structure signals in summary so the quality score can use them (P21 Req 2.4)
        validation_result["summary"]["has_imports"] = has_imports
        validation_result["summary"]["has_definitions"] = has_functions

        if not has_imports:
            validation_result["warnings"].append("No import statements found")  # Warning, not error

        if not has_functions:
            validation_result["warnings"].append("No functions or classes found")  # Warning, not error

        # Simple metrics for summary
        class_count = code.count("class ")
        method_count = code.count("def ")
        line_count = len([line for line in code.split("\n") if line.strip()])

        validation_result["summary"]["class_count"] = class_count
        validation_result["summary"]["method_count"] = method_count
        validation_result["summary"]["line_count"] = line_count

        # Only check for obvious placeholders - be permissive
        obvious_placeholders = ["TODO", "FIXME", "NotImplementedError", "pass  # TODO"]
        placeholder_count = sum(1 for pattern in obvious_placeholders if pattern in code)

        if placeholder_count > 0:
            validation_result["warnings"].append(f"Found {placeholder_count} obvious placeholder(s)")

        validation_result["summary"]["placeholder_count"] = placeholder_count

        # Surface PySpark anti-pattern findings supplied by the caller (P21 Req 2.6).
        # These are static checks only — no execution/sandboxing is introduced (Req 2.7).
        validation_result["summary"]["antipattern_count"] = antipattern_count
        if antipattern_count > 0:
            validation_result["warnings"].append(f"Found {antipattern_count} PySpark antipattern(s)")

        # Final validation - only fail on critical issues
        if validation_result["is_valid"]:
            validation_result["summary"]["status"] = "VALID"
            validation_result["summary"]["quality_score"] = _compute_quality_score(
                validation_result["summary"], antipattern_count=antipattern_count
            )
        else:
            validation_result["summary"]["status"] = "INVALID"
            validation_result["summary"]["quality_score"] = 0

        logger.info(f"Code validation summary: {validation_result['summary']}")

    except Exception as e:
        validation_result["errors"].append(f"Validation process error: {e!s}")
        validation_result["is_valid"] = False
        validation_result["summary"]["status"] = "ERROR"

    return validation_result


def extract_code_only(response_text: str) -> str:
    """
    Minimal code extraction - only remove obvious non-code content.

    Args:
        response_text: The full response from the agent

    Returns:
        str: The Python code with minimal processing
    """
    cleaned_code = response_text.strip()

    # Only remove markdown code blocks if present
    if "```python" in cleaned_code:
        # Extract content between python code blocks
        code_block_pattern = r"```python\s*(.*?)\s*```"
        matches = re.findall(code_block_pattern, cleaned_code, re.DOTALL)
        if matches:
            cleaned_code = matches[0].strip()
    elif "```" in cleaned_code:
        # Extract content between generic code blocks
        code_block_pattern = r"```\s*(.*?)\s*```"
        matches = re.findall(code_block_pattern, cleaned_code, re.DOTALL)
        if matches:
            cleaned_code = matches[0].strip()

    # Only remove very obvious introductory phrases (be conservative)
    if cleaned_code.startswith("Here's the code:") or cleaned_code.startswith("Here is the code:"):
        lines = cleaned_code.split("\n")
        cleaned_code = "\n".join(lines[1:]).strip()

    # Log the extraction for debugging
    logger.info(f"Code extraction: Original length: {len(response_text)}, Cleaned length: {len(cleaned_code)}")

    return cleaned_code
