"""The version, and which code runs: serverInfo carries the commit, and one line says it at start."""

from __future__ import annotations

import io
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_web_service import FakeFetcher, _service

from web_scraper import __version__
from web_scraper.mcp_server import McpServer, announce, build_server_info, startup_line

ROOT = Path(__file__).resolve().parents[1]


def _git_answering(stdout: str = "727ec4f\n", returncode: int = 0, raises: Exception | None = None):
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_the_version_is_1_2_0_in_the_package_and_in_pyproject() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert __version__ == "1.2.0"
    assert re.search(r'^version = "1\.2\.0"$', pyproject, re.MULTILINE)


class TestServerInfo:
    def test_a_git_checkout_carries_its_commit(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        run = _git_answering()

        info = build_server_info(tmp_path, run=run)

        assert info == {"name": "web-research", "version": "1.2.0+g727ec4f"}
        command, kwargs = run.calls[0]  # type: ignore[attr-defined]
        assert command[0] == "git" and "rev-parse" in command and "--short" in command
        assert str(tmp_path) in command
        assert kwargs["timeout"] == 1

    def test_a_worktree_has_a_git_file_instead_of_a_folder(self, tmp_path: Path) -> None:
        (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")

        info = build_server_info(tmp_path, run=_git_answering("abc1234\n"))

        assert info["version"] == "1.2.0+gabc1234"

    def test_without_a_checkout_it_is_the_plain_version_and_git_is_not_run(
        self, tmp_path: Path
    ) -> None:
        run = _git_answering()

        info = build_server_info(tmp_path, run=run)

        assert info == {"name": "web-research", "version": "1.2.0"}
        assert run.calls == []  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "run",
        [
            _git_answering(raises=FileNotFoundError("git")),
            _git_answering(raises=subprocess.TimeoutExpired("git", 1)),
            _git_answering(raises=OSError("denied")),
            _git_answering(returncode=128, stdout=""),
            _git_answering(stdout="\n"),
            _git_answering(stdout="not a sha\n"),
        ],
    )
    def test_git_failing_leaves_the_plain_version(self, tmp_path: Path, run: Any) -> None:
        (tmp_path / ".git").mkdir()

        assert build_server_info(tmp_path, run=run)["version"] == "1.2.0"

    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    def test_a_real_checkout_names_its_commit(self, tmp_path: Path) -> None:
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
               "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}  # fmt: skip
        for command in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", "-C", str(tmp_path), *command], check=True, env=env)

        version = build_server_info(tmp_path)["version"]

        assert re.fullmatch(r"1\.2\.0\+g[0-9a-f]{7,40}", version)

    @pytest.mark.asyncio
    async def test_initialize_answers_with_the_info_the_server_was_given(self) -> None:
        info = {"name": "web-research", "version": "1.2.0+g727ec4f"}
        server = McpServer(_service(FakeFetcher({})), server_info=info)

        first = await server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        second = await server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "initialize"})

        assert first["result"]["serverInfo"] == info  # type: ignore[index]
        assert second["result"]["serverInfo"] == info  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_without_info_the_server_says_the_plain_version(self) -> None:
        server = McpServer(_service(FakeFetcher({})))

        answer = await server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})

        assert answer["result"]["serverInfo"] == {  # type: ignore[index]
            "name": "web-research",
            "version": __version__,
        }


class TestStartupLine:
    def test_it_names_the_server_the_version_the_commit_and_python(self) -> None:
        line = startup_line({"name": "web-research", "version": "1.2.0+g727ec4f"})

        assert "\n" not in line
        assert line.startswith("web-research")
        assert "version=1.2.0+g727ec4f" in line
        assert "commit=727ec4f" in line
        assert f"python={platform.python_version()}" in line

    def test_without_a_commit_it_says_so(self) -> None:
        line = startup_line({"name": "web-research", "version": "1.2.0"})

        assert "version=1.2.0 " in line + " " and "commit=unknown" in line

    def test_it_is_one_line_on_the_stream_given(self) -> None:
        stream = io.StringIO()

        announce({"name": "web-research", "version": "1.2.0+g727ec4f"}, stream)

        assert stream.getvalue().count("\n") == 1
        assert "727ec4f" in stream.getvalue()
