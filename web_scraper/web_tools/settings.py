"""Environment-backed settings for the local web tools."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from web_scraper.local_env import load_local_env

_BROWSER_PREFERENCES = {"auto", "chrome", "chromium", "camoufox"}

# The smallest Markdown cap a scrape accepts. The request schema states it and
# the service refuses a smaller one, so no layer silently raises a caller's cap.
MIN_MARKDOWN_CHARS = 500

# The most links one web_scrape request may ask for (``max_links``); the server's
# ``scrape_max_links`` is the default, and ``max_links`` its own ceiling.
SCRAPE_MAX_LINKS_CEILING = 200

# web_scrape with ``urls``: how many pages one call reads, how many at a time from one host,
# and the bounds of the cap on the whole answer (``max_total_chars``).
SCRAPE_BATCH_MIN_URLS = 2
SCRAPE_BATCH_MAX_URLS = 10
SCRAPE_BATCH_PER_HOST = 2
SCRAPE_BATCH_MIN_TOTAL_CHARS = 5_000
SCRAPE_BATCH_MAX_TOTAL_CHARS = 500_000

# web_search with ``read``: how many of the top results one call reads, how much of each
# (the default and the ceiling), all of it inside the bound on the whole answer.
SEARCH_READ_MAX = 5
SEARCH_READ_CHARS_DEFAULT = 4_000
SEARCH_READ_CHARS_MAX = 20_000

# web_extract's bounds: the values one ``all`` field may return, and the size of
# a whole answer in characters (its compact JSON).
EXTRACT_MAX_ITEMS_CEILING = 1_000
# How far into a field's matches an ``all`` field may start (``offset``).
EXTRACT_OFFSET_CEILING = 1_000_000
EXTRACT_MAX_CHARS_FLOOR = 1_000
EXTRACT_MAX_CHARS_CEILING = 200_000


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


# What a search falls back to, in the order it asks: DuckDuckGo's lite page and Yahoo (both
# plain HTTP) before the headless browser.
DEFAULT_FALLBACK_PROVIDERS = ("google", "yahoo", "duckduckgo_lite")


def provider_names(values: Iterable[str]) -> list[str]:
    """Provider names, trimmed, lower-cased and unique; ``none`` and ``off`` name no provider."""
    names: list[str] = []
    for value in values:
        name = str(value).strip().lower()
        if name and name not in {"none", "off"} and name not in names:
            names.append(name)
    return names


def _fallback_chain_from_env() -> list[str]:
    """``..._FALLBACK_PROVIDERS`` (a list), else the older single ``..._FALLBACK_PROVIDER``."""
    raw: Optional[str] = os.getenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS")
    if raw is None:
        raw = os.getenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDER")
    if raw is None:
        return list(DEFAULT_FALLBACK_PROVIDERS)
    return provider_names(raw.split(","))


def _data_dir_from_env() -> str:
    """The folder the web tools keep their data in (crawl jobs): ``WEB_TOOLS_DATA_DIR``.

    Unset, it is ``web-research`` in the XDG data home (``$XDG_DATA_HOME``, else
    ``~/.local/share``). An empty value, ``none`` or ``off`` keeps nothing on disk.
    """
    raw = os.getenv("WEB_TOOLS_DATA_DIR")
    if raw is None:
        base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return str(Path(base).expanduser() / "web-research")
    raw = raw.strip()
    if raw.lower() in {"", "none", "off"}:
        return ""
    return str(Path(raw).expanduser())


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
    # How long the angles after the base may take once it answered; 0 waits for all.
    search_angle_budget_ms: int = 5_000
    search_region: str = "wt-wt"
    search_language: str = "en-US,en;q=0.9"
    search_cache_ttl_seconds: int = 300
    search_provider_concurrency: int = 1
    search_jitter_min_ms: int = 250
    search_jitter_max_ms: int = 500
    search_fallback_providers: list[str] = field(
        default_factory=lambda: list(DEFAULT_FALLBACK_PROVIDERS)
    )
    search_fallback_timeout_seconds: float = 60.0
    # A provider that meets a wall (a captcha, a bot check), or fails search_provider_failure_limit
    # times in a row, is skipped for search_provider_cooldown_seconds (10 minutes); a provider that
    # comes back and is refused again cools down twice as long, up to the maximum (an hour). The
    # window is how far back the recent calls that order the providers reach.
    search_provider_cooldown_seconds: int = 600
    search_provider_max_cooldown_seconds: int = 3_600
    search_provider_failure_limit: int = 3
    search_provider_window_seconds: int = 1_800
    # A deep search's primary-source angle is the one worth waiting for: it may run this long when
    # the side angles are cut at search_angle_budget_ms (0 gives it the same budget).
    search_primary_budget_ms: int = 15_000
    default_search_limit: int = 5
    max_search_limit: int = 20
    max_query_length: int = 500

    # Dating results by reading the head of their pages (web_search resolve_dates). "auto" does it
    # for a search that asks for news or a date range, "on" for every search that does not say
    # resolve_dates: false, "off" never, whatever a call says: the server then reads no page of a
    # search result on its own. It reads the first search_resolve_dates_bytes of at most the top
    # search_resolve_dates_top results (strict_dates may widen that to search_resolve_dates_max),
    # four at a time, and stops when search_resolve_dates_budget_ms is spent.
    search_resolve_dates: str = "auto"
    search_resolve_dates_top: int = 6
    search_resolve_dates_max: int = 12
    search_resolve_dates_budget_ms: int = 4_000
    search_resolve_dates_bytes: int = 65_536
    # A head that has not ended by then is read on to its end, up to this (cloud.google.com's are
    # 170 KB, their dates at 140 KB).
    search_resolve_dates_head_cap: int = 262_144
    search_resolve_dates_concurrency: int = 4

    fetch_timeout_seconds: float = 20.0
    fetch_retries: int = 1
    max_response_bytes: int = 5_000_000
    max_redirects: int = 5
    global_concurrency: int = 8
    host_concurrency: int = 2
    host_min_interval_ms: int = 200

    cache_ttl_seconds: int = 900
    cache_max_entries: int = 256
    # The fetched documents scrape and extract share (0 seconds turns it off), bounded.
    page_cache_ttl_seconds: int = 300
    page_cache_max_entries: int = 64
    page_cache_max_bytes: int = 20_000_000

    browser_enabled: bool = True
    browser_preference: str = "auto"
    browser_timeout_seconds: float = 30.0
    browser_idle_seconds: int = 120
    # After a page has loaded the browser waits at least this long, and then until the page's visible text
    # has stopped growing for browser_settle_stable_ms (and is more than a loading placeholder), but not
    # longer than browser_settle_max_ms (and never past browser_timeout_seconds for the whole render).
    browser_post_load_wait_ms: int = 500
    browser_settle_max_ms: int = 8_000
    browser_settle_stable_ms: int = 600
    browser_max_contexts: int = 4
    # A page whose readable text is shorter than this while its HTML has scripts is read again in the
    # browser; the text of a rendered page is a shell's under a quarter of it.
    browser_render_min_chars: int = 200
    # A page the browser could not read (a captcha, an access wall, a timeout) is read once more in Camoufox
    # when the package camoufox is installed and has fetched what it needs. false never does.
    browser_stealth: bool = True
    # What Camoufox lacks (its browser: about 100 MB to download and 2.3 GB on disk; the model its fingerprints are
    # drawn from; its ad blocker) is fetched by `python -m camoufox fetch`, once, in the background, the first time a
    # page needs it. false never fetches by itself: run that command yourself.
    browser_stealth_fetch: bool = True

    # Telling a page a site gives for an address it has nothing at (a soft 404) costs a request: a page that
    # does not say it is itself (no canonical link to its own address) makes the server ask its host, once
    # per session, for a path that cannot exist, and compare. False never asks.
    soft_404_check: bool = True

    markdown_max_chars: int = 50_000
    max_links: int = 500
    scrape_max_links: int = 50
    scrape_batch_concurrency: int = 4
    scrape_batch_max_total_chars: int = 60_000
    search_read_max_total_chars: int = 60_000
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

    # How long the walk through the Internet Archive's copies of a walled page may take.
    archive_budget_seconds: float = 20.0

    # Hosts added to the tables of first-party sites (official) and of aggregators (ranked below
    # the report they copy); each a host or a host and a path (apple.com/newsroom), a subdomain
    # included. The built-in tables are in sources.py.
    official_hosts: list[str] = field(default_factory=list)
    aggregator_hosts: list[str] = field(default_factory=list)

    # Reading a walled page another way (web_scrape recover): "on" does it for a page that answers
    # a captcha, a 401, 403 or 429 or a bot wall, unless the call says recover: false; "off" never
    # does, whatever a call says, so the server reads no page of its own choosing for a scrape.
    # The whole attempt (archive, AMP and print forms, feeds, other outlets' copies) stops after
    # recover_budget_seconds; recover_max_alternates is the most candidate addresses it lists.
    scrape_recover: str = "on"
    recover_budget_seconds: float = 30.0
    recover_max_alternates: int = 6

    # Where crawl jobs are kept so a restart does not lose them; empty keeps nothing on disk
    # (what a settings made by hand does: only from_env, the server's own, names a folder).
    data_dir: str = ""

    max_extract_fields: int = 50
    extract_max_items: int = 100
    extract_max_chars: int = 20_000

    def __post_init__(self) -> None:
        self.search_provider = (self.search_provider or "duckduckgo").strip().lower()
        self.search_timeout_seconds = max(self.search_timeout_seconds, 1.0)
        depth = (self.search_default_depth or "discover").strip().lower()
        self.search_default_depth = depth if depth in {"fast", "discover", "deep"} else "discover"
        self.search_angle_delay_ms = min(max(self.search_angle_delay_ms, 0), 5_000)
        self.search_angle_budget_ms = min(max(self.search_angle_budget_ms, 0), 120_000)
        region = (self.search_region or "wt-wt").strip().lower()
        self.search_region = region if re.fullmatch(r"[a-z]{2}-[a-z]{2}|wt-wt", region) else "wt-wt"
        language = (self.search_language or "").strip()
        self.search_language = (
            language if re.fullmatch(r"[A-Za-z0-9,;=._*\- ]{1,128}", language) else "en-US,en;q=0.9"
        )
        self.search_cache_ttl_seconds = max(self.search_cache_ttl_seconds, 1)
        self.search_provider_concurrency = max(self.search_provider_concurrency, 1)
        self.search_jitter_min_ms = min(max(self.search_jitter_min_ms, 0), 10_000)
        self.search_jitter_max_ms = min(
            max(self.search_jitter_max_ms, self.search_jitter_min_ms),
            10_000,
        )
        self.search_fallback_providers = provider_names(self.search_fallback_providers or [])
        self.search_fallback_timeout_seconds = max(self.search_fallback_timeout_seconds, 1.0)
        self.search_provider_cooldown_seconds = max(self.search_provider_cooldown_seconds, 0)
        self.search_provider_max_cooldown_seconds = max(
            self.search_provider_max_cooldown_seconds,
            self.search_provider_cooldown_seconds,
        )
        self.search_provider_failure_limit = max(self.search_provider_failure_limit, 1)
        self.search_provider_window_seconds = max(self.search_provider_window_seconds, 60)
        self.search_primary_budget_ms = min(max(self.search_primary_budget_ms, 0), 300_000)
        self.default_search_limit = max(self.default_search_limit, 1)
        self.max_search_limit = max(self.max_search_limit, self.default_search_limit)
        self.max_query_length = max(self.max_query_length, 1)
        mode = (self.search_resolve_dates or "auto").strip().lower()
        self.search_resolve_dates = mode if mode in {"auto", "on", "off"} else "auto"
        self.search_resolve_dates_top = min(max(self.search_resolve_dates_top, 1), 20)
        self.search_resolve_dates_max = min(
            max(self.search_resolve_dates_max, self.search_resolve_dates_top), 20
        )
        self.search_resolve_dates_budget_ms = min(
            max(self.search_resolve_dates_budget_ms, 0), 60_000
        )
        self.search_resolve_dates_bytes = min(max(self.search_resolve_dates_bytes, 4_096), 262_144)
        self.search_resolve_dates_head_cap = min(
            max(self.search_resolve_dates_head_cap, self.search_resolve_dates_bytes), 1_048_576
        )
        self.search_resolve_dates_concurrency = min(
            max(self.search_resolve_dates_concurrency, 1), 8
        )
        self.fetch_timeout_seconds = max(self.fetch_timeout_seconds, 1.0)
        self.fetch_retries = max(self.fetch_retries, 0)
        self.max_response_bytes = max(self.max_response_bytes, 1024)
        self.max_redirects = max(self.max_redirects, 0)
        self.global_concurrency = max(self.global_concurrency, 1)
        self.host_concurrency = max(self.host_concurrency, 1)
        self.host_min_interval_ms = max(self.host_min_interval_ms, 0)
        self.cache_ttl_seconds = max(self.cache_ttl_seconds, 1)
        self.cache_max_entries = max(self.cache_max_entries, 1)
        self.page_cache_ttl_seconds = max(self.page_cache_ttl_seconds, 0)
        self.page_cache_max_entries = max(self.page_cache_max_entries, 1)
        self.page_cache_max_bytes = max(self.page_cache_max_bytes, 1_000)

        preference = (self.browser_preference or "auto").strip().lower()
        self.browser_preference = preference if preference in _BROWSER_PREFERENCES else "auto"
        self.browser_timeout_seconds = max(self.browser_timeout_seconds, 1.0)
        self.browser_idle_seconds = max(self.browser_idle_seconds, 5)
        self.browser_post_load_wait_ms = max(self.browser_post_load_wait_ms, 0)
        self.browser_settle_max_ms = max(self.browser_settle_max_ms, 0)
        self.browser_settle_stable_ms = max(self.browser_settle_stable_ms, 0)
        self.browser_max_contexts = max(self.browser_max_contexts, 1)
        self.browser_render_min_chars = max(self.browser_render_min_chars, 20)

        self.markdown_max_chars = max(self.markdown_max_chars, MIN_MARKDOWN_CHARS)
        self.max_links = max(self.max_links, 1)
        self.scrape_max_links = min(max(self.scrape_max_links, 0), self.max_links)
        self.scrape_batch_concurrency = max(self.scrape_batch_concurrency, 1)
        self.scrape_batch_max_total_chars = min(
            max(self.scrape_batch_max_total_chars, SCRAPE_BATCH_MIN_TOTAL_CHARS),
            SCRAPE_BATCH_MAX_TOTAL_CHARS,
        )
        self.search_read_max_total_chars = min(
            max(self.search_read_max_total_chars, SCRAPE_BATCH_MIN_TOTAL_CHARS),
            SCRAPE_BATCH_MAX_TOTAL_CHARS,
        )
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
        self.archive_budget_seconds = max(self.archive_budget_seconds, 0.1)
        self.scrape_recover = (
            "off"
            if (self.scrape_recover or "on").strip().lower()
            in {
                "off",
                "false",
                "0",
                "no",
            }
            else "on"
        )
        self.recover_budget_seconds = min(max(self.recover_budget_seconds, 1.0), 300.0)
        self.recover_max_alternates = min(max(self.recover_max_alternates, 1), 20)
        self.official_hosts = [host for host in self.official_hosts if host and host.strip()]
        self.aggregator_hosts = [host for host in self.aggregator_hosts if host and host.strip()]
        self.max_extract_fields = max(self.max_extract_fields, 1)
        self.extract_max_items = min(max(self.extract_max_items, 1), EXTRACT_MAX_ITEMS_CEILING)
        self.extract_max_chars = min(
            max(self.extract_max_chars, EXTRACT_MAX_CHARS_FLOOR),
            EXTRACT_MAX_CHARS_CEILING,
        )

    @classmethod
    def from_env(cls) -> WebToolsSettings:
        """Build settings from WEB_TOOLS_* environment variables."""
        load_local_env()
        return cls(
            search_provider=os.getenv("WEB_TOOLS_SEARCH_PROVIDER", "duckduckgo"),
            search_timeout_seconds=_float("WEB_TOOLS_SEARCH_TIMEOUT_SECONDS", 30.0),
            search_default_depth=os.getenv("WEB_TOOLS_SEARCH_DEPTH", "discover"),
            search_angle_delay_ms=_int("WEB_TOOLS_SEARCH_ANGLE_DELAY_MS", 150),
            search_angle_budget_ms=_int("WEB_TOOLS_SEARCH_ANGLE_BUDGET_MS", 5_000),
            search_region=os.getenv("WEB_TOOLS_SEARCH_REGION", "wt-wt"),
            search_language=os.getenv(
                "WEB_TOOLS_SEARCH_LANGUAGE",
                "en-US,en;q=0.9",
            ),
            search_cache_ttl_seconds=_int("WEB_TOOLS_SEARCH_CACHE_TTL_SECONDS", 300),
            search_provider_concurrency=_int("WEB_TOOLS_SEARCH_PROVIDER_CONCURRENCY", 1),
            search_jitter_min_ms=_int("WEB_TOOLS_SEARCH_JITTER_MIN_MS", 250),
            search_jitter_max_ms=_int("WEB_TOOLS_SEARCH_JITTER_MAX_MS", 500),
            search_fallback_providers=_fallback_chain_from_env(),
            search_fallback_timeout_seconds=_float(
                "WEB_TOOLS_SEARCH_FALLBACK_TIMEOUT_SECONDS",
                60.0,
            ),
            search_provider_cooldown_seconds=_int(
                "WEB_TOOLS_SEARCH_PROVIDER_COOLDOWN_SECONDS",
                600,
            ),
            search_provider_max_cooldown_seconds=_int(
                "WEB_TOOLS_SEARCH_PROVIDER_MAX_COOLDOWN_SECONDS",
                3_600,
            ),
            search_provider_failure_limit=_int("WEB_TOOLS_SEARCH_PROVIDER_FAILURE_LIMIT", 3),
            search_provider_window_seconds=_int("WEB_TOOLS_SEARCH_PROVIDER_WINDOW_SECONDS", 1_800),
            search_primary_budget_ms=_int("WEB_TOOLS_SEARCH_PRIMARY_BUDGET_MS", 15_000),
            default_search_limit=_int("WEB_TOOLS_DEFAULT_SEARCH_LIMIT", 5),
            max_search_limit=_int("WEB_TOOLS_MAX_SEARCH_LIMIT", 20),
            max_query_length=_int("WEB_TOOLS_MAX_QUERY_LENGTH", 500),
            search_resolve_dates=os.getenv("WEB_TOOLS_SEARCH_RESOLVE_DATES", "auto"),
            search_resolve_dates_top=_int("WEB_TOOLS_SEARCH_RESOLVE_DATES_TOP", 6),
            search_resolve_dates_max=_int("WEB_TOOLS_SEARCH_RESOLVE_DATES_MAX", 12),
            search_resolve_dates_budget_ms=_int("WEB_TOOLS_SEARCH_RESOLVE_DATES_BUDGET_MS", 4_000),
            search_resolve_dates_bytes=_int("WEB_TOOLS_SEARCH_RESOLVE_DATES_BYTES", 65_536),
            search_resolve_dates_head_cap=_int("WEB_TOOLS_SEARCH_RESOLVE_DATES_HEAD_CAP", 262_144),
            search_resolve_dates_concurrency=_int("WEB_TOOLS_SEARCH_RESOLVE_DATES_CONCURRENCY", 4),
            fetch_timeout_seconds=_float("WEB_TOOLS_FETCH_TIMEOUT_SECONDS", 20.0),
            fetch_retries=_int("WEB_TOOLS_FETCH_RETRIES", 1),
            max_response_bytes=_int("WEB_TOOLS_MAX_RESPONSE_BYTES", 5_000_000),
            max_redirects=_int("WEB_TOOLS_MAX_REDIRECTS", 5),
            global_concurrency=_int("WEB_TOOLS_GLOBAL_CONCURRENCY", 8),
            host_concurrency=_int("WEB_TOOLS_HOST_CONCURRENCY", 2),
            host_min_interval_ms=_int("WEB_TOOLS_HOST_MIN_INTERVAL_MS", 200),
            cache_ttl_seconds=_int("WEB_TOOLS_CACHE_TTL_SECONDS", 900),
            cache_max_entries=_int("WEB_TOOLS_CACHE_MAX_ENTRIES", 256),
            page_cache_ttl_seconds=_int("WEB_TOOLS_PAGE_CACHE_TTL_SECONDS", 300),
            page_cache_max_entries=_int("WEB_TOOLS_PAGE_CACHE_MAX_ENTRIES", 64),
            page_cache_max_bytes=_int("WEB_TOOLS_PAGE_CACHE_MAX_BYTES", 20_000_000),
            browser_enabled=_bool("WEB_TOOLS_BROWSER_ENABLED", True),
            browser_preference=os.getenv("WEB_TOOLS_BROWSER_PREFERENCE", "auto"),
            browser_timeout_seconds=_float("WEB_TOOLS_BROWSER_TIMEOUT_SECONDS", 30.0),
            browser_idle_seconds=_int("WEB_TOOLS_BROWSER_IDLE_SECONDS", 120),
            browser_post_load_wait_ms=_int("WEB_TOOLS_BROWSER_POST_LOAD_WAIT_MS", 500),
            browser_settle_max_ms=_int("WEB_TOOLS_BROWSER_SETTLE_MAX_MS", 8_000),
            browser_settle_stable_ms=_int("WEB_TOOLS_BROWSER_SETTLE_STABLE_MS", 600),
            browser_max_contexts=_int("WEB_TOOLS_BROWSER_MAX_CONTEXTS", 4),
            browser_render_min_chars=_int("WEB_TOOLS_BROWSER_RENDER_MIN_CHARS", 200),
            browser_stealth=_bool("WEB_TOOLS_BROWSER_STEALTH", True),
            browser_stealth_fetch=_bool("WEB_TOOLS_BROWSER_STEALTH_FETCH", True),
            soft_404_check=_bool("WEB_TOOLS_SOFT_404_CHECK", True),
            markdown_max_chars=_int("WEB_TOOLS_MARKDOWN_MAX_CHARS", 50_000),
            max_links=_int("WEB_TOOLS_MAX_LINKS", 500),
            scrape_max_links=_int("WEB_TOOLS_SCRAPE_MAX_LINKS", 50),
            scrape_batch_concurrency=_int("WEB_TOOLS_SCRAPE_BATCH_CONCURRENCY", 4),
            scrape_batch_max_total_chars=_int("WEB_TOOLS_SCRAPE_BATCH_MAX_TOTAL_CHARS", 60_000),
            search_read_max_total_chars=_int("WEB_TOOLS_SEARCH_READ_MAX_TOTAL_CHARS", 60_000),
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
            archive_budget_seconds=_float("WEB_TOOLS_ARCHIVE_BUDGET_SECONDS", 20.0),
            scrape_recover=os.getenv("WEB_TOOLS_SCRAPE_RECOVER", "on"),
            recover_budget_seconds=_float("WEB_TOOLS_RECOVER_BUDGET_SECONDS", 30.0),
            recover_max_alternates=_int("WEB_TOOLS_RECOVER_MAX_ALTERNATES", 6),
            official_hosts=_csv("WEB_TOOLS_OFFICIAL_HOSTS"),
            aggregator_hosts=_csv("WEB_TOOLS_AGGREGATOR_HOSTS"),
            data_dir=_data_dir_from_env(),
            max_extract_fields=_int("WEB_TOOLS_MAX_EXTRACT_FIELDS", 50),
            extract_max_items=_int("WEB_TOOLS_EXTRACT_MAX_ITEMS", 100),
            extract_max_chars=_int("WEB_TOOLS_EXTRACT_MAX_CHARS", 20_000),
        )
