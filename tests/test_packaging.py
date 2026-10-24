"""The package a fresh Dot install brings: dotagent-web-research, with a web-research-mcp command.

``web-scraper`` and ``web-research-mcp`` are taken on PyPI by others, so the distribution is
``dotagent-web-research``; the import package stays ``web_scraper``.
"""

from __future__ import annotations

import importlib
import re
import stat
from pathlib import Path

import pytest
import yaml

import web_scraper

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _section(name: str) -> str:
    match = re.search(rf"^\[{re.escape(name)}\]\n(.*?)(?=^\[|\Z)", PYPROJECT, re.M | re.S)
    assert match, f"no [{name}] in pyproject.toml"
    return match.group(1)


class TestMetadata:
    def test_the_distribution_is_dotagent_web_research_at_the_packages_version(self) -> None:
        project = _section("project")

        assert re.search(r'^name = "dotagent-web-research"$', project, re.M)
        assert re.search(rf'^version = "{re.escape(web_scraper.__version__)}"$', project, re.M)
        assert web_scraper.__version__ == "1.3.1"

    def test_the_import_package_stays_web_scraper(self) -> None:
        packages = _section("tool.setuptools.packages.find")

        assert 'include = ["web_scraper*"]' in packages
        assert (ROOT / "web_scraper" / "mcp_server.py").is_file()

    def test_python_9_is_still_enough(self) -> None:
        assert re.search(r'^requires-python = ">=3\.9"$', _section("project"), re.M)

    def test_the_description_says_what_it_is(self) -> None:
        description = re.search(r'^description = "(.*)"$', _section("project"), re.M)

        assert description is not None
        text = description.group(1).lower()
        assert "mcp" in text and "dot" in text and "search" in text

    def test_no_dependency_was_added(self) -> None:
        block = re.search(r"^dependencies = \[(.*?)^\]", _section("project"), re.M | re.S)

        assert block is not None
        names = re.findall(r'"([A-Za-z0-9_.\-]+)', block.group(1))
        assert names == [
            "httpx",
            "curl_cffi",
            "beautifulsoup4",
            "lxml",
            "defusedxml",
            "fastapi",
            "uvicorn",
            "pydantic",
            "python-dotenv",
            "click",
            "tqdm",
            "pypdf",
        ]


class TestCommands:
    def test_the_server_has_a_command_and_the_old_one_stays(self) -> None:
        scripts = _section("project.scripts")

        assert 'web-research-mcp = "web_scraper.mcp_server:main"' in scripts
        assert 'web-scraper = "web_scraper.cli:main"' in scripts

    @pytest.mark.parametrize("entry", ["web_scraper.mcp_server:main", "web_scraper.cli:main"])
    def test_each_command_resolves_to_a_function(self, entry: str) -> None:
        module, function = entry.split(":")

        assert callable(getattr(importlib.import_module(module), function))

    def test_the_launcher_of_the_owners_checkout_stays(self) -> None:
        launcher = ROOT / "scripts" / "web-research-mcp"

        assert launcher.is_file() and launcher.stat().st_mode & stat.S_IXUSR
        assert "web_scraper.mcp_server" in launcher.read_text(encoding="utf-8")


class TestRelease:
    @pytest.fixture()
    def workflow(self) -> dict:
        path = ROOT / ".github" / "workflows" / "release.yml"
        assert path.is_file(), "no release workflow"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_it_runs_on_a_version_tag_and_nothing_else(self, workflow: dict) -> None:
        triggers = workflow.get("on") or workflow.get(True)

        assert triggers == {"push": {"tags": ["v*"]}}

    def test_it_builds_then_publishes_with_trusted_publishing(self, workflow: dict) -> None:
        jobs = workflow["jobs"]
        publish = jobs["publish"]
        steps = " ".join(str(step) for step in publish["steps"])

        assert "build" in jobs and publish["needs"] == "build"
        environment = publish["environment"]
        assert (environment if isinstance(environment, str) else environment["name"]) == "pypi"
        assert publish["permissions"]["id-token"] == "write"
        assert re.search(r"pypa/gh-action-pypi-publish@[0-9a-f]{40}", steps)
        # No token and no password: the publisher is trusted by pypi.org, not by a secret.
        assert "password" not in steps and "secrets." not in steps

    def test_the_build_checks_the_tag_against_the_version_and_keeps_its_rights_small(
        self, workflow: dict
    ) -> None:
        build = workflow["jobs"]["build"]
        steps = " ".join(str(step) for step in build["steps"])

        assert "GITHUB_REF_NAME" in steps and "__version__" in steps
        assert "build" in steps
        assert workflow.get("permissions", {}).get("contents") == "read"
        assert "id-token" not in (build.get("permissions") or {})


WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
# ``uses: owner/repo@<40 hex>  # v1.2.3``: the commit that runs, and the release it is.
PINNED_USE = re.compile(
    r"^\s*(?:- )?uses: (?P<action>[\w.-]+/[\w./-]+)@(?P<sha>[0-9a-f]{40}) # v\d+\.\d+\.\d+\s*$"
)


class TestWorkflowsArePinned:
    """A tag can be moved, a commit cannot: what a workflow runs with the right to publish is a commit."""

    def test_the_workflows_are_there(self) -> None:
        assert {path.name for path in WORKFLOWS} >= {"ci.yml", "release.yml"}

    @pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
    def test_every_action_is_a_full_commit_with_its_release_in_a_comment(self, path: Path) -> None:
        uses = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if re.match(r"^\s*(?:- )?uses:", line)
        ]

        assert uses, f"{path.name} uses no action"
        unpinned = [line.strip() for line in uses if not PINNED_USE.match(line)]
        assert unpinned == []

    def test_the_release_workflow_uses_the_five_actions_it_needs_and_no_other(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        used = {
            match.group("action") for line in text.splitlines() if (match := PINNED_USE.match(line))
        }

        assert used == {
            "actions/checkout",
            "actions/setup-python",
            "actions/upload-artifact",
            "actions/download-artifact",
            "pypa/gh-action-pypi-publish",
        }

    def test_the_build_tools_of_the_release_are_at_fixed_versions(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        install = re.search(r"python -m pip install (?P<packages>.+)", text)

        assert install is not None
        packages = install.group("packages").split()
        assert sorted(re.sub(r"==.*", "", name) for name in packages) == ["build", "twine"]
        assert all(re.fullmatch(r"[a-z]+==\d+(\.\d+)+", name) for name in packages), packages


class TestReadme:
    def test_it_says_how_to_install_and_run_it(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        assert "pip install dotagent-web-research" in readme
        assert "uv tool install dotagent-web-research" in readme
        assert "web-research-mcp" in readme
        assert "Quick Start" in readme  # the rest stays


class TestCli:
    def test_the_cli_reports_the_packages_version_whatever_the_distribution_is_called(self) -> None:
        from click.testing import CliRunner

        from web_scraper.cli import main

        result = CliRunner().invoke(main, ["--version"])

        assert result.exit_code == 0
        assert result.output.strip() == f"web-scraper, version {web_scraper.__version__}"
