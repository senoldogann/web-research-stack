"""The REST app of the web tools around a real service, for the tests that ask what an answer looks like on the wire.

``tests/test_web_api.py`` puts a fake behind the routes and tests the routing; the response models of the routes
drop what they do not declare, so a field a service adds is only there for a client if a model says it. These
tests put the real service behind the same routes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web_scraper.api import create_app
from web_scraper.config import config
from web_scraper.web_tools.service import WebToolsService

HEADERS = {"X-API-Key": "test-key"}


def rest_client(monkeypatch: pytest.MonkeyPatch, service: WebToolsService) -> TestClient:
    monkeypatch.setattr(config, "api_keys", ["test-key"], raising=False)
    monkeypatch.setattr(config, "api_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_web_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_allowed_origins", [], raising=False)
    monkeypatch.setattr(config, "api_trusted_hosts", ["testserver"], raising=False)
    monkeypatch.setattr(config, "history_db_path", None, raising=False)
    app = create_app(config)
    app.state.web_tools = service
    return TestClient(app)
