#!/usr/bin/env python3
import os
import subprocess
import sys

# Add scripts dir to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from common_utils import (
    file_exists,
    print_fail,
    print_header,
    print_info,
    print_success,
    print_warning,
)

ROOT_DIR = os.getcwd()


def audit_nodejs():
    if file_exists("package.json"):
        print_info("Analyzing Node.js dependencies...")
        try:
            # Check for generic 'any' usage in TS/JS if possible?
            # For now, let's stick to dependency audit.
            result = subprocess.run(["npm", "audit", "--json"], capture_output=True, text=True)
            # npm audit returns non-zero for found vulnerabilities
            if result.returncode == 0:
                print_success("Node.js audit: No vulnerabilities found.")
                return True
            else:
                print_warning("Node.js audit: Found potential vulnerabilities.")
                # Fail the build so CI surfaces dependency issues
                return False
        except FileNotFoundError:
            print_warning("npm command not found. Skipping Node.js audit.")
    return True


def audit_python():
    has_requirements = file_exists("requirements.txt")
    has_pyproject = file_exists("pyproject.toml")
    if has_requirements or has_pyproject:
        print_info("Analyzing Python dependencies...")
        try:
            # Requires pip-audit to be installed
            if has_requirements:
                cmd = ["pip-audit", "-r", "requirements.txt"]
            else:
                # pyproject.toml — let pip-audit auto-detect the project
                cmd = ["pip-audit"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                print_success("Python audit: No vulnerabilities found.")
                return True
            else:
                print_warning("Python audit: Found potential vulnerabilities.")
                return False
        except FileNotFoundError:
            print_warning("pip-audit command not found. Skipping Python audit.")
    return True


def audit_go():
    if file_exists("go.mod"):
        print_info("Analyzing Go modules...")
        try:
            result = subprocess.run(["go", "list", "-m", "all"], capture_output=True, text=True)
            if result.returncode == 0:
                print_success("Go modules: Successfully listed.")
                return True
        except FileNotFoundError:
            print_warning("go command not found. Skipping Go audit.")
    return True


def extract_files_from_codebase():
    codebase_path = os.path.join(ROOT_DIR, "CODEBASE.md")
    if not file_exists(codebase_path):
        return []

    files = []
    try:
        with open(codebase_path) as f:
            for line in f:
                if "├──" in line or "└──" in line:
                    name = line.split("─")[-1].split("#")[0].strip()
                    if name and not name.endswith("/"):
                        files.append(name)
    except Exception:
        pass
    return files


def audit_structure():
    print_info("Auditing Codebase Structure (Map vs. Territory)...")
    codebase_files = extract_files_from_codebase()
    if not codebase_files:
        return True  # Skip if no codebase map exists

    all_ok = True
    for f in codebase_files:
        # Simplified check for major files
        if f in ["CODEBASE.md", "AGENTS.md", "README.md"]:
            if not file_exists(os.path.join(ROOT_DIR, f)):
                print_warning(f"File listed in CODEBASE.md but missing: {f}")
                all_ok = False
    return all_ok


def main():
    print_header("MAESTRO DEPENDENCY & STRUCTURE AUDITOR (v2.3.1)")

    success = True
    # 1. Structural Audit
    success &= audit_structure()

    # 2. Dependency Audit
    print("\n")
    success &= audit_nodejs()
    success &= audit_python()
    success &= audit_go()

    if success:
        print_success("\nAll audits completed successfully.")
        sys.exit(0)
    else:
        print_fail("\nAudits found inconsistencies or security risks.")
        sys.exit(1)


if __name__ == "__main__":
    main()
