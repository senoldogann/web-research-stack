"""Environment-backed settings for the local web tools."""

from __future__ import annotations

import os
from dataclasses import dataclass

import dotenv

_BROWSER_PREFERENCES = {"auto", "chrome", "chromium", "camoufox"}


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _csv(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class WebToolsSettings:
    """Knobs for search, fetching, caching, browsers, mapping, and crawling."""

    search_provider: str = "duckduckgo"
    search_timeout_seconds: float = 30.0
    search_default_depth: str = "discover"
    search_angle_delay_ms: int = 150
    search_provider_concurrency: int = 1
    search_jitter_min_ms: int = 250
    search_jitter_max_ms: int = 500
    search_fallback_provider: str = "google"
    search_fallback_timeout_seconds: float = 60.0
    search_provider_cooldown_seconds: int = 5
    search_provider_max_cooldown_seconds: int = 300
    default_search_limit: int = 5
    max_search_limit: int = 20
    max_query_length: int = 500

    fetch_timeout_seconds: float = 20.0
    fetch_retries: int = 1
    max_response_bytes: int = 5_000_000
    max_redirects: int = 5
    global_concurrency: int = 8
    host_concurrency: int = 2
    host_min_interval_ms: int = 200

    cache_ttl_seconds: int = 900
    cache_max_entries: int = 256

    browser_enabled: bool = True
    browser_preference: str = "auto"
    browser_timeout_seconds: float = 30.0
    browser_idle_seconds: int = 120
    browser_post_load_wait_ms: int = 500
    browser_max_contexts: int = 4
    browser_render_min_chars: int = 200

    markdown_max_chars: int = 50_000
    max_links: int = 500
    default_map_limit: int = 500
    max_map_limit: int = 5_000
    max_sitemaps: int = 25

    respect_robots: bool = True
    robots_cache_ttl_seconds: int = 3_600

    default_crawl_pages: int = 20
    max_crawl_pages: int = 100
    default_crawl_depth: int = 2
    max_crawl_depth: int = 4
    crawl_concurrency: int = 4
    crawl_content_limit_chars: int = 12_000
    crawl_job_ttl_seconds: int = 3_600
    max_crawl_jobs: int = 32
    max_crawl_errors: int = 100

    max_extract_fields: int = 50

    def __post_init__(self) -> None:
        self.search_provider = (self.search_provider or "duckduckgo").strip().lower()
        self.search_timeout_seconds = max(self.search_timeout_seconds, 1.0)
        depth = (self.search_default_depth or "discover").strip().lower()
        self.search_default_depth = depth if depth in {"fast", "discover", "deep"} else "discover"
        self.search_angle_delay_ms = min(max(self.search_angle_delay_ms, 0), 5_000)
        self.search_provider_concurrency = max(self.search_provider_concurrency, 1)
        self.search_jitter_min_ms = min(max(self.search_jitter_min_ms, 0), 10_000)
        self.search_jitter_max_ms = min(
            max(self.search_jitter_max_ms, self.search_jitter_min_ms),
            10_000,
        )
        fallback = (self.search_fallback_provider or "").strip().lower()
        self.search_fallback_provider = "" if fallback in {"", "none", "off"} else fallback
        self.search_fallback_timeout_seconds = max(self.search_fallback_timeout_seconds, 1.0)
        self.search_provider_cooldown_seconds = max(self.search_provider_cooldown_seconds, 0)
        self.search_provider_max_cooldown_seconds = max(
            self.search_provider_max_cooldown_seconds,
            self.search_provider_cooldown_seconds,
        )
        self.default_search_limit = max(self.default_search_limit, 1)
        self.max_search_limit = max(self.max_search_limit, self.default_search_limit)
        self.max_query_length = max(self.max_query_length, 1)
        self.fetch_timeout_seconds = max(self.fetch_timeout_seconds, 1.0)
        self.fetch_retries = max(self.fetch_retries, 0)
        self.max_response_bytes = max(self.max_response_bytes, 1024)
        self.max_redirects = max(self.max_redirects, 0)
        self.global_concurrency = max(self.global_concurrency, 1)
        self.host_concurrency = max(self.host_concurrency, 1)
        self.host_min_interval_ms = max(self.host_min_interval_ms, 0)
        self.cache_ttl_seconds = max(self.cache_ttl_seconds, 1)
        self.cache_max_entries = max(self.cache_max_entries, 1)

        preference = (self.browser_preference or "auto").strip().lower()
        self.browser_preference = preference if preference in _BROWSER_PREFERENCES else "auto"
        self.browser_timeout_seconds = max(self.browser_timeout_seconds, 1.0)
        self.browser_idle_seconds = max(self.browser_idle_seconds, 5)
        self.browser_post_load_wait_ms = max(self.browser_post_load_wait_ms, 0)
        self.browser_max_contexts = max(self.browser_max_contexts, 1)
        self.browser_render_min_chars = max(self.browser_render_min_chars, 20)

        self.markdown_max_chars = max(self.markdown_max_chars, 500)
        self.max_links = max(self.max_links, 1)
        self.default_map_limit = max(self.default_map_limit, 1)
        self.max_map_limit = max(self.max_map_limit, self.default_map_limit)
        self.max_sitemaps = max(self.max_sitemaps, 1)
        self.robots_cache_ttl_seconds = max(self.robots_cache_ttl_seconds, 60)

        self.max_crawl_pages = max(self.max_crawl_pages, 1)
        self.default_crawl_pages = min(
            max(self.default_crawl_pages, 1),
            self.max_crawl_pages,
        )
        self.max_crawl_depth = max(self.max_crawl_depth, 0)
        self.default_crawl_depth = min(
            max(self.default_crawl_depth, 0),
            self.max_crawl_depth,
        )
        self.crawl_concurrency = max(self.crawl_concurrency, 1)
        self.crawl_content_limit_chars = max(self.crawl_content_limit_chars, 500)
        self.crawl_job_ttl_seconds = max(self.crawl_job_ttl_seconds, 60)
        self.max_crawl_jobs = max(self.max_crawl_jobs, 1)
        self.max_crawl_errors = max(self.max_crawl_errors, 1)
        self.max_extract_fields = max(self.max_extract_fields, 1)

    @classmethod
    def from_env(cls) -> WebToolsSettings:
        """Build settings from WEB_TOOLS_* environment variables."""
        dotenv.load_dotenv()
        return cls(
            search_provider=os.getenv("WEB_TOOLS_SEARCH_PROVIDER", "duckduckgo"),
            search_timeout_seconds=_float("WEB_TOOLS_SEARCH_TIMEOUT_SECONDS", 30.0),
            search_default_depth=os.getenv("WEB_TOOLS_SEARCH_DEPTH", "discover"),
            search_angle_delay_ms=_int("WEB_TOOLS_SEARCH_ANGLE_DELAY_MS", 150),
            search_provider_concurrency=_int("WEB_TOOLS_SEARCH_PROVIDER_CONCURRENCY", 1),
            search_jitter_min_ms=_int("WEB_TOOLS_SEARCH_JITTER_MIN_MS", 250),
            search_jitter_max_ms=_int("WEB_TOOLS_SEARCH_JITTER_MAX_MS", 500),
            search_fallback_provider=os.getenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDER", "google"),
            search_fallback_timeout_seconds=_float(
                "WEB_TOOLS_SEARCH_FALLBACK_TIMEOUT_SECONDS",
                60.0,
            ),
            search_provider_cooldown_seconds=_int(
                "WEB_TOOLS_SEARCH_PROVIDER_COOLDOWN_SECONDS",
                5,
            ),
            search_provider_max_cooldown_seconds=_int(
                "WEB_TOOLS_SEARCH_PROVIDER_MAX_COOLDOWN_SECONDS",
                300,
            ),
            default_search_limit=_int("WEB_TOOLS_DEFAULT_SEARCH_LIMIT", 5),
            max_search_limit=_int("WEB_TOOLS_MAX_SEARCH_LIMIT", 20),
            max_query_length=_int("WEB_TOOLS_MAX_QUERY_LENGTH", 500),
            fetch_timeout_seconds=_float("WEB_TOOLS_FETCH_TIMEOUT_SECONDS", 20.0),
            fetch_retries=_int("WEB_TOOLS_FETCH_RETRIES", 1),
            max_response_bytes=_int("WEB_TOOLS_MAX_RESPONSE_BYTES", 5_000_000),
            max_redirects=_int("WEB_TOOLS_MAX_REDIRECTS", 5),
            global_concurrency=_int("WEB_TOOLS_GLOBAL_CONCURRENCY", 8),
            host_concurrency=_int("WEB_TOOLS_HOST_CONCURRENCY", 2),
            host_min_interval_ms=_int("WEB_TOOLS_HOST_MIN_INTERVAL_MS", 200),
            cache_ttl_seconds=_int("WEB_TOOLS_CACHE_TTL_SECONDS", 900),
            cache_max_entries=_int("WEB_TOOLS_CACHE_MAX_ENTRIES", 256),
            browser_enabled=_bool("WEB_TOOLS_BROWSER_ENABLED", True),
            browser_preference=os.getenv("WEB_TOOLS_BROWSER_PREFERENCE", "auto"),
            browser_timeout_seconds=_float("WEB_TOOLS_BROWSER_TIMEOUT_SECONDS", 30.0),
            browser_idle_seconds=_int("WEB_TOOLS_BROWSER_IDLE_SECONDS", 120),
            browser_post_load_wait_ms=_int("WEB_TOOLS_BROWSER_POST_LOAD_WAIT_MS", 500),
            browser_max_contexts=_int("WEB_TOOLS_BROWSER_MAX_CONTEXTS", 4),
            browser_render_min_chars=_int("WEB_TOOLS_BROWSER_RENDER_MIN_CHARS", 200),
            markdown_max_chars=_int("WEB_TOOLS_MARKDOWN_MAX_CHARS", 50_000),
            max_links=_int("WEB_TOOLS_MAX_LINKS", 500),
            default_map_limit=_int("WEB_TOOLS_DEFAULT_MAP_LIMIT", 500),
            max_map_limit=_int("WEB_TOOLS_MAX_MAP_LIMIT", 5_000),
            max_sitemaps=_int("WEB_TOOLS_MAX_SITEMAPS", 25),
            respect_robots=_bool("WEB_TOOLS_RESPECT_ROBOTS", True),
            robots_cache_ttl_seconds=_int("WEB_TOOLS_ROBOTS_CACHE_TTL_SECONDS", 3_600),
            default_crawl_pages=_int("WEB_TOOLS_DEFAULT_CRAWL_PAGES", 20),
            max_crawl_pages=_int("WEB_TOOLS_MAX_CRAWL_PAGES", 100),
            default_crawl_depth=_int("WEB_TOOLS_DEFAULT_CRAWL_DEPTH", 2),
            max_crawl_depth=_int("WEB_TOOLS_MAX_CRAWL_DEPTH", 4),
            crawl_concurrency=_int("WEB_TOOLS_CRAWL_CONCURRENCY", 4),
            crawl_content_limit_chars=_int("WEB_TOOLS_CRAWL_CONTENT_LIMIT_CHARS", 12_000),
            crawl_job_ttl_seconds=_int("WEB_TOOLS_CRAWL_JOB_TTL_SECONDS", 3_600),
            max_crawl_jobs=_int("WEB_TOOLS_MAX_CRAWL_JOBS", 32),
            max_crawl_errors=_int("WEB_TOOLS_MAX_CRAWL_ERRORS", 100),
            max_extract_fields=_int("WEB_TOOLS_MAX_EXTRACT_FIELDS", 50),
        )
