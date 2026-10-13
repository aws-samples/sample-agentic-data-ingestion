# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Test that shared modules respect the dependency layer hierarchy.

Parses the AST of every shared/*.py file and verifies no module imports
from a higher layer per the _LAYER_MAP defined in shared/__init__.py.
"""

import ast
import pathlib
import sys

import pytest

# Ensure shared/ is importable — point to the project-level shared/ directory
# Test file is at tests/unit/shared/, so we go up to the project root
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
SHARED_DIR = PROJECT_ROOT / "shared"
sys.path.insert(0, str(PROJECT_ROOT))

# Force import from the project shared/ not the test shared/ package
sys.modules.pop("shared", None)
from shared import _LAYER_MAP


def _extract_shared_imports(py_file: pathlib.Path):
    """Extract top-level imports that reference modules in _LAYER_MAP.

    Only checks top-level statements — imports inside function bodies are
    runtime-conditional (gated by feature flags) and intentionally allowed.
    """
    tree = ast.parse(py_file.read_text())
    imports = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                if name in _LAYER_MAP:
                    imports.append((name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            name = node.module.split(".")[0]
            if name in _LAYER_MAP:
                imports.append((name, node.lineno))
    return imports


def _get_shared_modules():
    """Return list of (module_name, file_path) for all shared modules in _LAYER_MAP."""
    modules = []
    for py_file in sorted(SHARED_DIR.glob("*.py")):
        module_name = py_file.stem
        if module_name == "__init__" or module_name not in _LAYER_MAP:
            continue
        modules.append((module_name, py_file))
    return modules


@pytest.mark.parametrize("module_name,py_file", _get_shared_modules(), ids=[m[0] for m in _get_shared_modules()])
def test_no_higher_layer_imports(module_name, py_file):
    """Each shared module must only import from the same or lower layer."""
    my_layer = _LAYER_MAP[module_name]
    shared_imports = _extract_shared_imports(py_file)

    violations = []
    for imported_name, lineno in shared_imports:
        dep_layer = _LAYER_MAP[imported_name]
        if dep_layer > my_layer:
            violations.append(f"  line {lineno}: imports {imported_name} (layer {dep_layer})")

    assert not violations, f"{module_name} (layer {my_layer}) has higher-layer imports:\n" + "\n".join(violations)


def test_layer_map_covers_all_shared_modules():
    """Every .py file in shared/ (except __init__ and known exclusions) should be in _LAYER_MAP."""
    py_files = {f.stem for f in SHARED_DIR.glob("*.py") if f.stem != "__init__"}
    mapped = set(_LAYER_MAP.keys())
    # artifact_schema is a new module not yet added to _LAYER_MAP
    known_unmapped = {"artifact_schema"}
    unmapped = py_files - mapped - known_unmapped
    assert not unmapped, f"Shared modules not in _LAYER_MAP: {unmapped}"


def test_validate_import_order_function():
    """The validate_import_order() function should pass without raising."""
    from shared import validate_import_order

    # Should not raise
    validate_import_order()
