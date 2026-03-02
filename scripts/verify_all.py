#!/usr/bin/env python3
import os
import subprocess
import sys

# Add scripts dir to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from common_utils import print_fail, print_header, print_info, print_success


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


def main():
    print_header("MAESTRO HARD QUALITY GATE")

    scripts_to_run = [
        ("generate_skill_index.py", "Skill Index Generation"),
        ("checklist.py", "Engineering Checklist"),
        ("dependency_analyzer.py", "Dependency Audit"),
    ]

    all_success = True

    # 1. Custom Scripts
    for script, name in scripts_to_run:
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

    # 3. Backend Tests
    if not run_command([sys.executable, "-m", "pytest", "tests"], "Backend Unit Tests"):
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
