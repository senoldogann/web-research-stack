"""robots.txt policy for crawl URL scheduling."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError

_USER_AGENT = "WebResearchTools"


@dataclass
class _RobotsEntry:
    parser: Optional[RobotFileParser]
    allow_all: bool
    disallow_all: bool
    expires_at: float


class RobotsPolicy:
    """Cache and apply robots.txt rules per origin; fail closed on server errors."""

    def __init__(self, fetcher: Any, cache_ttl_seconds: int = 3600) -> None:
        self._fetcher = fetcher
        self._ttl = max(cache_ttl_seconds, 60)
        self._entries: dict[str, _RobotsEntry] = {}

    async def allowed(self, url: str) -> bool:
        origin = _origin(url)
        entry = self._entries.get(origin)
        now = time.monotonic()
        if entry is None or entry.expires_at <= now:
            entry = await self._load(url)
            self._entries[origin] = entry
        if entry.disallow_all:
            return False
        if entry.allow_all or entry.parser is None:
            return True
        return entry.parser.can_fetch(_USER_AGENT, url)

    async def _load(self, url: str) -> _RobotsEntry:
        robots_url = urljoin(_origin(url), "robots.txt")
        expires_at = time.monotonic() + self._ttl
        try:
            result = await self._fetcher.fetch(robots_url)
        except FetchError as exc:
            status = exc.status_code or 0
            disallow = status in {401, 403} or 500 <= status < 600
            return _RobotsEntry(
                None, allow_all=not disallow, disallow_all=disallow, expires_at=expires_at
            )
        except WebToolError:
            return _RobotsEntry(None, allow_all=True, disallow_all=False, expires_at=expires_at)

        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.parse(result.html.splitlines())
        except Exception:  # noqa: BLE001 - malformed robots means no usable rules
            return _RobotsEntry(None, allow_all=True, disallow_all=False, expires_at=expires_at)
        return _RobotsEntry(parser, allow_all=False, disallow_all=False, expires_at=expires_at)


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
