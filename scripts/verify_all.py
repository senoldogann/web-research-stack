#!/usr/bin/env python3
import os
import subprocess
import sys

# Add scripts dir to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from common_utils import print_fail, print_header, print_info, print_success

# ---------------------------------------------------------------------------
# Release gate thresholds
# ---------------------------------------------------------------------------

# Minimum number of tests that must pass (prevents silent test deletion)
MIN_PASSING_TESTS: int = 30

# Key module files that must exist for the quality pipeline to be intact
REQUIRED_MODULE_FILES: list[tuple[str, str]] = [
    ("web_scraper/research/citation_verifier.py", "Citation Faithfulness Verifier"),
    ("web_scraper/research/retry_utils.py", "Network Retry Utilities"),
    ("web_scraper/research/profile_collectors.py", "Profile Source Collectors"),
    ("web_scraper/research/agent.py", "Research Agent"),
    ("web_scraper/research/ranking.py", "Search Result Ranking"),
]


def run_command(command, name, cwd=None):
    print_info(f"Running {name}...")
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, cwd=cwd, shell=isinstance(command, str)
        )
        if result.returncode == 0:
            print_success(f"{name} passed.")
            return True
        else:
            print_fail(f"{name} failed.")
            print(result.stdout)
            print(result.stderr)
            return False
    except Exception as e:
        print_fail(f"Error running {name}: {e}")
        return False


def check_test_count(min_passing: int) -> bool:
    """Run pytest and verify at least *min_passing* tests pass."""
    print_info(f"Running test count gate (min {min_passing} passing tests)...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "--tb=no"],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        # Pytest summary line: "N passed" or "N passed, M warnings"
        import re
        match = re.search(r"(\d+) passed", output)
        if match:
            count = int(match.group(1))
            if count >= min_passing:
                print_success(f"Test count gate passed: {count} tests passing (min {min_passing}).")
                return True
            else:
                print_fail(
                    f"Test count gate FAILED: only {count} tests passing, need at least {min_passing}."
                )
                return False
        else:
            # No passing tests found in output ⇒ tests may have all failed
            print_fail(f"Test count gate FAILED: could not determine passing count from:\n{output[:500]}")
            return False
    except Exception as e:
        print_fail(f"Test count gate error: {e}")
        return False


def check_required_files() -> bool:
    """Verify that all required quality-pipeline module files exist."""
    print_info("Checking required module files...")
    ok = True
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    for rel_path, description in REQUIRED_MODULE_FILES:
        abs_path = os.path.join(repo_root, rel_path)
        if os.path.exists(abs_path):
            print_success(f"  ✓ {description} ({rel_path})")
        else:
            print_fail(f"  ✗ {description} MISSING: {rel_path}")
            ok = False
    return ok


def check_module_imports() -> bool:
    """Quick import smoke-test for the new quality modules."""
    print_info("Running module import smoke test...")
    modules_to_import = [
        "web_scraper.research.citation_verifier",
        "web_scraper.research.retry_utils",
        "web_scraper.research.profile_collectors",
    ]
    ok = True
    for mod in modules_to_import:
        try:
            result = subprocess.run(
                [sys.executable, "-c", f"import {mod}; print('ok')"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print_success(f"  ✓ import {mod}")
            else:
                print_fail(f"  ✗ import {mod} failed:\n{result.stderr[:300]}")
                ok = False
        except Exception as e:
            print_fail(f"  ✗ import {mod} error: {e}")
            ok = False
    return ok


def main():
    print_header("MAESTRO HARD QUALITY GATE")

    scripts_to_run = [
        ("generate_skill_index.py", "Skill Index Generation"),
        ("checklist.py", "Engineering Checklist"),
        ("dependency_analyzer.py", "Dependency Audit"),
    ]

    all_success = True

    # 0. Release Gate — required files
    if not check_required_files():
        all_success = False

    # 0b. Release Gate — module imports
    if not check_module_imports():
        all_success = False

    # 1. Custom Scripts
    for script, name in scripts_to_run:
        script_path = os.path.join("scripts", script)
        if not os.path.exists(script_path):
            print_info(f"Skipping {name} (missing: {script_path}).")
            continue
        if not run_command([sys.executable, f"scripts/{script}"], name):
            all_success = False

    # 2. Code Quality (Lint)
    if not run_command(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "web_scraper",
            "scripts",
            "tests",
            "--exclude",
            ".venv,build,dist,venv",
        ],
        "Ruff Linting",
    ):
        all_success = False

    # 3. Backend Tests (with count gate)
    if not run_command([sys.executable, "-m", "pytest", "tests"], "Backend Unit Tests"):
        all_success = False
    if not check_test_count(MIN_PASSING_TESTS):
        all_success = False

    # 4. Frontend Build
    web_ui_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web-ui"))
    if os.path.exists(web_ui_path):
        if not run_command(["npm", "run", "build"], "Frontend Build (Next.js)", cwd=web_ui_path):
            all_success = False

    if all_success:
        print_header("FINAL VERIFICATION: ALL SYSTEMS GREEN")
        sys.exit(0)
    else:
        print_header("FINAL VERIFICATION: FAILED - FIX ISSUES BEFORE COMMIT")
        sys.exit(1)


if __name__ == "__main__":
    main()
