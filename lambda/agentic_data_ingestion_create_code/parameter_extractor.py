#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Extract required parameters from generated PySpark code"""

import re
from typing import Any


def extract_required_parameters(code: str) -> dict[str, Any]:
    """
    Extract required and optional parameters from generated PySpark code.

    Looks for patterns like:
    - REQUIRED_PARAMS = ['PARAM1', 'PARAM2']
    - getResolvedOptions(sys.argv, ['PARAM1', 'PARAM2'])

    Returns:
        Dictionary with required_params, optional_params, and parameter_defaults
    """
    required_params = []
    optional_params = []
    parameter_defaults = {}

    # Pattern 1: REQUIRED_PARAMS = [...]
    required_pattern = r"REQUIRED_PARAMS\s*=\s*\[(.*?)\]"
    match = re.search(required_pattern, code, re.DOTALL)
    if match:
        params_str = match.group(1)
        # Extract quoted strings
        required_params = re.findall(r"['\"]([^'\"]+)['\"]", params_str)

    # Pattern 2: OPTIONAL_PARAMS = [...]
    optional_pattern = r"OPTIONAL_PARAMS\s*=\s*\[(.*?)\]"
    match = re.search(optional_pattern, code, re.DOTALL)
    if match:
        params_str = match.group(1)
        optional_params = re.findall(r"['\"]([^'\"]+)['\"]", params_str)

    # Pattern 3: getResolvedOptions(sys.argv, [...])
    if not required_params:
        resolved_pattern = r"getResolvedOptions\s*\(\s*sys\.argv\s*,\s*\[(.*?)\]"
        match = re.search(resolved_pattern, code, re.DOTALL)
        if match:
            params_str = match.group(1)
            required_params = re.findall(r"['\"]([^'\"]+)['\"]", params_str)

    # Pattern 4: args.get('PARAM', 'default_value')
    default_pattern = r"args\.get\(['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]"
    for match in re.finditer(default_pattern, code):
        param_name = match.group(1)
        default_value = match.group(2)
        parameter_defaults[param_name] = default_value

    return {
        "required_params": list(set(required_params)),
        "optional_params": list(set(optional_params)),
        "parameter_defaults": parameter_defaults,
        "total_params": len(set(required_params + optional_params)),
    }


def validate_glue_job_arguments(glue_job_arguments: dict[str, str], required_params: list[str]) -> dict[str, Any]:
    """
    Validate that Glue job DefaultArguments match the code's required parameters.

    Returns:
        Dictionary with validation results and missing/extra parameters
    """
    # Convert Glue argument keys (--PARAM) to parameter names (PARAM)
    provided_params = {key.lstrip("--"): value for key, value in glue_job_arguments.items()}

    missing_params = []
    empty_params = []

    for param in required_params:
        if param not in provided_params:
            missing_params.append(param)
        elif not provided_params[param] or provided_params[param].strip() == "":
            empty_params.append(param)

    is_valid = len(missing_params) == 0 and len(empty_params) == 0

    return {
        "is_valid": is_valid,
        "missing_params": missing_params,
        "empty_params": empty_params,
        "provided_params": list(provided_params.keys()),
        "validation_message": (
            "All required parameters are provided" if is_valid else f"Missing: {missing_params}, Empty: {empty_params}"
        ),
    }
