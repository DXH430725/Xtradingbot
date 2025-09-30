#!/usr/bin/env python3
"""System validation script for XBot architecture compliance."""

import sys
import ast
import os
from pathlib import Path
from typing import Dict, List, Tuple


def count_lines_of_code(file_path: Path) -> int:
    """Count non-empty, non-comment lines of code."""
    if not file_path.exists() or file_path.suffix != '.py':
        return 0

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Parse AST to count actual code lines
        tree = ast.parse(content)

        # Count lines with actual code (not just comments/docstrings)
        lines_with_code = set()
        for node in ast.walk(tree):
            if hasattr(node, 'lineno'):
                lines_with_code.add(node.lineno)

        return len(lines_with_code)

    except Exception as e:
        print(f"Warning: Could not parse {file_path}: {e}")
        # Fallback to simple line counting
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            code_lines = 0
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    code_lines += 1

            return code_lines
        except:
            return 0


def analyze_directory_structure(xbot_path: Path) -> Dict[str, any]:
    """Analyze the xbot directory structure."""
    if not xbot_path.exists():
        return {"error": f"Directory {xbot_path} does not exist"}

    analysis = {
        "total_files": 0,
        "python_files": 0,
        "production_files": 0,  # Exclude tests and scripts
        "total_loc": 0,
        "core_loc": 0,
        "files": [],
        "violations": []
    }

    # Core directories that count toward the <2k LOC limit
    core_dirs = {"core", "execution", "connector"}

    # Exclude test directories and script files from file count constraint
    excluded_from_count = {"tests"}

    for root, dirs, files in os.walk(xbot_path):
        for file in files:
            if file.endswith('.py'):
                file_path = Path(root) / file
                relative_path = file_path.relative_to(xbot_path)

                loc = count_lines_of_code(file_path)
                analysis["total_files"] += 1
                analysis["python_files"] += 1
                analysis["total_loc"] += loc

                # Check if it's a core file
                is_core = any(str(relative_path).startswith(core_dir) for core_dir in core_dirs)

                # Check if it should be excluded from file count constraint
                is_excluded = any(str(relative_path).startswith(excluded_dir) for excluded_dir in excluded_from_count)

                if not is_excluded:
                    analysis["production_files"] += 1

                if is_core:
                    analysis["core_loc"] += loc

                file_info = {
                    "path": str(relative_path),
                    "loc": loc,
                    "is_core": is_core,
                    "is_excluded": is_excluded
                }
                analysis["files"].append(file_info)

                # Check file size constraint (≤600 lines) - only for production files
                if not is_excluded and loc > 600:
                    analysis["violations"].append(f"File {relative_path} has {loc} LOC (>600)")

    # Check core LOC constraint (<2000 lines)
    if analysis["core_loc"] >= 2000:
        analysis["violations"].append(f"Core components have {analysis['core_loc']} LOC (≥2000)")

    # Check production file count constraint (≤18 files, excluding tests)
    if analysis["production_files"] > 18:
        analysis["violations"].append(f"Production files: {analysis['production_files']} (>18, excluding tests)")

    return analysis


def check_key_components(xbot_path: Path) -> List[str]:
    """Check that key components exist."""
    violations = []

    required_files = [
        "execution/router.py",
        "execution/order_model.py",
        "execution/tracking_limit.py",
        "execution/services/market_data_service.py",
        "execution/services/order_service.py",
        "execution/services/risk_service.py",
        "connector/interface.py",
        "connector/mock_connector.py",
        "strategy/smoke_test.py",
        "core/trading_core.py",
        "core/clock.py",
        "app/main.py",
        "app/config.py",
    ]

    for required_file in required_files:
        file_path = xbot_path / required_file
        if not file_path.exists():
            violations.append(f"Missing required file: {required_file}")

    return violations


def validate_imports(xbot_path: Path) -> List[str]:
    """Basic validation of Python imports."""
    violations = []

    try:
        # Try importing key modules
        sys.path.insert(0, str(xbot_path.parent))

        # Test core imports
        from xbot.core import TradingCore
        from xbot.execution.router import ExecutionRouter
        from xbot.execution.order_model import Order
        from xbot.connector.interface import IConnector
        from xbot.connector.mock_connector import MockConnector
        from xbot.strategy.smoke_test import SmokeTestStrategy
        from xbot.app.main import main

        print("✅ All key imports successful")

    except ImportError as e:
        violations.append(f"Import error: {e}")
    except Exception as e:
        violations.append(f"Import validation error: {e}")

    return violations


def main():
    """Main validation function."""
    print("=== XBot Architecture Validation ===\n")

    # Find xbot directory
    current_dir = Path.cwd()
    xbot_path = current_dir / "xbot"

    if not xbot_path.exists():
        print(f"❌ XBot directory not found at {xbot_path}")
        return 1

    # Analyze structure
    print("1. Analyzing directory structure...")
    analysis = analyze_directory_structure(xbot_path)

    if "error" in analysis:
        print(f"❌ {analysis['error']}")
        return 1

    print(f"   Total Python files: {analysis['python_files']}")
    print(f"   Production files: {analysis['production_files']} (excluding tests)")
    print(f"   Total LOC: {analysis['total_loc']}")
    print(f"   Core LOC: {analysis['core_loc']}")

    # Check constraints
    print("\n2. Checking architecture constraints...")

    constraint_violations = analysis["violations"]

    if not constraint_violations:
        print("   ✅ All constraints satisfied")
    else:
        print(f"   ❌ {len(constraint_violations)} constraint violation(s):")
        for violation in constraint_violations:
            print(f"     - {violation}")

    # Check key components
    print("\n3. Checking required components...")
    component_violations = check_key_components(xbot_path)

    if not component_violations:
        print("   ✅ All required components present")
    else:
        print(f"   ❌ {len(component_violations)} missing component(s):")
        for violation in component_violations:
            print(f"     - {violation}")

    # Validate imports
    print("\n4. Validating imports...")
    import_violations = validate_imports(xbot_path)

    if not import_violations:
        print("   ✅ Import validation passed")
    else:
        print(f"   ❌ {len(import_violations)} import issue(s):")
        for violation in import_violations:
            print(f"     - {violation}")

    # File details
    if len(sys.argv) > 1 and sys.argv[1] == "--details":
        print("\n5. File details:")
        for file_info in sorted(analysis["files"], key=lambda x: x["loc"], reverse=True):
            core_marker = " [CORE]" if file_info["is_core"] else ""
            excluded_marker = " [TEST]" if file_info["is_excluded"] else ""
            print(f"   {file_info['path']}: {file_info['loc']} LOC{core_marker}{excluded_marker}")

    # Summary
    total_violations = len(constraint_violations) + len(component_violations) + len(import_violations)

    print(f"\n=== Validation Summary ===")
    if total_violations == 0:
        print("🎉 System validation PASSED")
        print("Architecture complies with design constraints.")
        return 0
    else:
        print(f"💥 System validation FAILED ({total_violations} issues)")
        return 1


if __name__ == "__main__":
    sys.exit(main())