"""The .env a run reads: the working folder's only, and none with PYTHON_DOTENV_DISABLED."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from web_scraper.local_env import load_local_env
from web_scraper.web_tools.settings import WebToolsSettings

_NAMES = (
    "WR_LOCAL_ENV_HERE",
    "WR_LOCAL_ENV_ABOVE",
    "WEB_TOOLS_SEARCH_PROVIDER",
    "PYTHON_DOTENV_DISABLED",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """load_dotenv writes os.environ itself: the names these tests read are unset before and after."""
    for name in _NAMES:
        monkeypatch.delenv(name, raising=False)
    yield
    for name in _NAMES:
        os.environ.pop(name, None)


def _folders(tmp_path: Path) -> Path:
    """A folder to work in, below one whose .env names what must not be read."""
    above = tmp_path / "above"
    here = above / "here"
    here.mkdir(parents=True)
    (above / ".env").write_text(
        "WR_LOCAL_ENV_ABOVE=1\nWEB_TOOLS_SEARCH_PROVIDER=planted\n", encoding="utf-8"
    )
    return here


def test_a_dotenv_above_the_working_folder_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_folders(tmp_path))

    assert load_local_env() is False
    assert "WR_LOCAL_ENV_ABOVE" not in os.environ
    assert WebToolsSettings.from_env().search_provider == "duckduckgo"


def test_the_working_folders_dotenv_is_read_and_overrides_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = _folders(tmp_path)
    (here / ".env").write_text(
        "WR_LOCAL_ENV_HERE=1\nWEB_TOOLS_SEARCH_PROVIDER=yahoo\n", encoding="utf-8"
    )
    monkeypatch.chdir(here)
    monkeypatch.setenv("WEB_TOOLS_SEARCH_PROVIDER", "google")

    assert load_local_env() is True
    assert os.environ["WR_LOCAL_ENV_HERE"] == "1"
    assert os.environ["WEB_TOOLS_SEARCH_PROVIDER"] == "google"


def test_python_dotenv_disabled_reads_no_dotenv_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = _folders(tmp_path)
    (here / ".env").write_text("WR_LOCAL_ENV_HERE=1\n", encoding="utf-8")
    monkeypatch.chdir(here)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")

    load_local_env()

    assert "WR_LOCAL_ENV_HERE" not in os.environ
