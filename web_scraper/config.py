"""Configuration management for the web scraper."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

import dotenv


def _parse_bool(value: Optional[str], default: bool) -> bool:
    """Parse common environment boolean formats."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: Optional[str]) -> List[str]:
    """Parse comma-separated environment values."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    """Clamp integer values into a safe inclusive range."""
    return max(minimum, min(int(value), maximum))


def _clamp_float(value: float, minimum: float) -> float:
    """Clamp float values to a safe minimum."""
    return max(float(value), minimum)


def _safe_int(value: Optional[str], default: int) -> int:
    """Parse an integer env var, falling back to *default* on any error."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _safe_float(value: Optional[str], default: float) -> float:
    """Parse a float env var, falling back to *default* on any error."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


@dataclass
class Config:
    """Configuration settings for the web scraper."""

    timeout: float = 30.0
    user_agent: Optional[str] = None
    follow_redirects: bool = True
    scraper_allow_private_networks: bool = False
    scraper_max_redirects: int = 5
    max_links: int = 100
    max_images: int = 50
    rate_limit: Optional[float] = None
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    api_allowed_origins: List[str] = field(default_factory=list)
    api_trusted_hosts: List[str] = field(default_factory=list)
    api_keys: List[str] = field(default_factory=list)
    api_rate_limit_per_minute: int = 60
    api_scrape_rate_limit_per_minute: int = 60
    api_research_rate_limit_per_minute: int = 15
    api_max_concurrent_requests: int = 10
    api_max_request_bytes: int = 65536
    research_max_concurrent_sources: int = 5
    research_timeout_per_source: float = 30.0
    ollama_host: str = "http://localhost:11434"
    default_research_model: str = "gpt-oss:120b-cloud"
    research_normal_auto_min_sources: int = 1
    research_normal_auto_max_sources: int = 15
    research_deep_min_sources: int = 15
    research_deep_max_sources: int = 50
    research_default_normal_source_target: int = 5
    research_default_deep_source_target: int = 20
    research_search_pool_extra_normal: int = 5
    research_search_pool_extra_deep: int = 10
    research_normal_content_limit_chars: int = 4000
    research_deep_content_limit_chars: int = 8000
    research_non_deep_source_char_cap: int = 10000
    research_planning_timeout_seconds: float = 30.0
    research_source_selection_timeout_seconds: float = 60.0
    research_synthesis_timeout_seconds: float = 120.0
    research_deep_synthesis_timeout_seconds: float = 240.0
    research_enable_query_rewrite: bool = True
    research_query_rewrite_max_variants: int = 4
    research_query_rewrite_timeout_seconds: float = 20.0
    research_strict_deep_mode: bool = True
    research_evidence_gate_enabled: bool = True
    research_retry_aggressive_enabled: bool = True
    research_intent_router_v2: bool = True
    research_enable_google_fallback: bool = True
    research_google_fallback_min_results: int = 5
    research_rerank_domain_diversity_boost: float = 0.25
    research_rerank_same_domain_penalty: float = 0.08
    research_rerank_exact_query_boost: float = 0.15
    duckduckgo_request_timeout_seconds: float = 30.0
    duckduckgo_request_delay_seconds: float = 0.5
    google_request_timeout_seconds: float = 30.0
    flaresolverr_enabled: bool = True
    flaresolverr_url: str = "http://web-research-flaresolverr:8191/v1"
    flaresolverr_fallback_urls: List[str] = field(
        default_factory=lambda: [
            "http://localhost:8191/v1",
            "http://web-research-flaresolverr-dev:8191/v1",
        ]
    )
    flaresolverr_request_timeout_seconds: float = 45.0
    flaresolverr_max_timeout_ms: int = 60000
    flaresolverr_max_attempts: int = 2
    flaresolverr_retry_backoff_seconds: float = 1.0
    max_query_length: int = 500
    max_source_content_chars: int = 12000
    cache_ttl_seconds: int = 300
    cache_max_entries: int = 256
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_seconds: int = 30
    history_db_path: Optional[str] = "web_scraper_history.sqlite3"
    metrics_enabled: bool = True
    app_env: str = "production"
    scraper_max_raw_text_chars: int = 50000

    def __post_init__(self) -> None:
        """Normalize env-backed runtime knobs into safe ranges."""
        self.timeout = _clamp_float(self.timeout, 1.0)
        self.scraper_max_redirects = _clamp_int(self.scraper_max_redirects, 0, 20)
        self.max_links = _clamp_int(self.max_links, 1, 10000)
        self.max_images = _clamp_int(self.max_images, 1, 10000)
        self.api_port = _clamp_int(self.api_port, 1, 65535)
        self.api_rate_limit_per_minute = _clamp_int(self.api_rate_limit_per_minute, 1, 100000)
        self.api_scrape_rate_limit_per_minute = _clamp_int(
            self.api_scrape_rate_limit_per_minute,
            1,
            100000,
        )
        self.api_research_rate_limit_per_minute = _clamp_int(
            self.api_research_rate_limit_per_minute,
            1,
            1000000,
        )
        self.api_max_concurrent_requests = _clamp_int(self.api_max_concurrent_requests, 1, 1000)
        self.api_max_request_bytes = _clamp_int(self.api_max_request_bytes, 128, 10_485_760)
        self.research_max_concurrent_sources = _clamp_int(
            self.research_max_concurrent_sources,
            1,
            100,
        )
        self.research_timeout_per_source = _clamp_float(self.research_timeout_per_source, 1.0)
        self.ollama_host = self.ollama_host.rstrip("/") or "http://localhost:11434"
        self.default_research_model = self.default_research_model.strip() or "gpt-oss:120b-cloud"

        self.research_normal_auto_min_sources = _clamp_int(
            self.research_normal_auto_min_sources,
            1,
            50,
        )
        self.research_normal_auto_max_sources = _clamp_int(
            self.research_normal_auto_max_sources,
            self.research_normal_auto_min_sources,
            50,
        )
        self.research_deep_min_sources = _clamp_int(
            self.research_deep_min_sources,
            1,
            50,
        )
        self.research_deep_max_sources = _clamp_int(
            self.research_deep_max_sources,
            self.research_deep_min_sources,
            50,
        )
        self.research_default_normal_source_target = _clamp_int(
            self.research_default_normal_source_target,
            self.research_normal_auto_min_sources,
            self.research_normal_auto_max_sources,
        )
        self.research_default_deep_source_target = _clamp_int(
            self.research_default_deep_source_target,
            self.research_deep_min_sources,
            self.research_deep_max_sources,
        )
        self.research_search_pool_extra_normal = _clamp_int(
            self.research_search_pool_extra_normal,
            0,
            50,
        )
        self.research_search_pool_extra_deep = _clamp_int(
            self.research_search_pool_extra_deep,
            0,
            50,
        )
        self.research_normal_content_limit_chars = _clamp_int(
            self.research_normal_content_limit_chars,
            500,
            100000,
        )
        self.research_deep_content_limit_chars = _clamp_int(
            self.research_deep_content_limit_chars,
            self.research_normal_content_limit_chars,
            200000,
        )
        self.research_non_deep_source_char_cap = _clamp_int(
            self.research_non_deep_source_char_cap,
            1000,
            200000,
        )
        self.research_planning_timeout_seconds = _clamp_float(
            self.research_planning_timeout_seconds,
            1.0,
        )
        self.research_source_selection_timeout_seconds = _clamp_float(
            self.research_source_selection_timeout_seconds,
            1.0,
        )
        self.research_synthesis_timeout_seconds = _clamp_float(
            self.research_synthesis_timeout_seconds,
            1.0,
        )
        self.research_deep_synthesis_timeout_seconds = _clamp_float(
            self.research_deep_synthesis_timeout_seconds,
            self.research_synthesis_timeout_seconds,
        )
        self.research_query_rewrite_max_variants = _clamp_int(
            self.research_query_rewrite_max_variants,
            1,
            8,
        )
        self.research_query_rewrite_timeout_seconds = _clamp_float(
            self.research_query_rewrite_timeout_seconds,
            1.0,
        )
        self.research_google_fallback_min_results = _clamp_int(
            self.research_google_fallback_min_results,
            1,
            50,
        )
        self.research_rerank_domain_diversity_boost = _clamp_float(
            self.research_rerank_domain_diversity_boost,
            0.0,
        )
        self.research_rerank_same_domain_penalty = _clamp_float(
            self.research_rerank_same_domain_penalty,
            0.0,
        )
        self.research_rerank_exact_query_boost = _clamp_float(
            self.research_rerank_exact_query_boost,
            0.0,
        )
        self.duckduckgo_request_timeout_seconds = _clamp_float(
            self.duckduckgo_request_timeout_seconds,
            1.0,
        )
        self.duckduckgo_request_delay_seconds = _clamp_float(
            self.duckduckgo_request_delay_seconds,
            0.0,
        )
        self.flaresolverr_url = (
            self.flaresolverr_url.strip() or "http://web-research-flaresolverr:8191/v1"
        )
        self.flaresolverr_fallback_urls = [u for u in self.flaresolverr_fallback_urls if u]
        self.flaresolverr_request_timeout_seconds = _clamp_float(
            self.flaresolverr_request_timeout_seconds,
            1.0,
        )
        self.flaresolverr_max_timeout_ms = _clamp_int(self.flaresolverr_max_timeout_ms, 1000, 300000)
        self.flaresolverr_max_attempts = _clamp_int(self.flaresolverr_max_attempts, 1, 5)
        self.flaresolverr_retry_backoff_seconds = _clamp_float(
            self.flaresolverr_retry_backoff_seconds,
            0.0,
        )

        self.max_query_length = _clamp_int(self.max_query_length, 3, 10000)
        self.max_source_content_chars = _clamp_int(self.max_source_content_chars, 1000, 200000)
        self.cache_ttl_seconds = _clamp_int(self.cache_ttl_seconds, 1, 86400)
        self.cache_max_entries = _clamp_int(self.cache_max_entries, 1, 100000)
        self.circuit_breaker_failure_threshold = _clamp_int(
            self.circuit_breaker_failure_threshold,
            1,
            1000,
        )
        self.circuit_breaker_recovery_seconds = _clamp_int(
            self.circuit_breaker_recovery_seconds,
            1,
            86400,
        )
        if self.history_db_path == "":
            self.history_db_path = None

    def __repr__(self) -> str:
        masked = [f"***{k[-4:]}" if len(k) > 4 else "***" for k in self.api_keys]
        return (
            f"Config(api_host={self.api_host!r}, api_port={self.api_port}, "
            f"app_env={self.app_env!r}, api_keys={masked})"
        )

    @classmethod
    def from_env(cls) -> Config:
        """Load configuration from environment variables."""
        dotenv.load_dotenv()

        return cls(
            timeout=_safe_float(os.getenv("SCRAPER_TIMEOUT"), 30.0),
            user_agent=os.getenv("SCRAPER_USER_AGENT"),
            follow_redirects=_parse_bool(os.getenv("SCRAPER_FOLLOW_REDIRECTS"), True),
            scraper_allow_private_networks=_parse_bool(
                os.getenv("SCRAPER_ALLOW_PRIVATE_NETWORKS"),
                False,
            ),
            scraper_max_redirects=_safe_int(os.getenv("SCRAPER_MAX_REDIRECTS"), 5),
            max_links=_safe_int(os.getenv("SCRAPER_MAX_LINKS"), 100),
            max_images=_safe_int(os.getenv("SCRAPER_MAX_IMAGES"), 50),
            rate_limit=_safe_float(os.getenv("SCRAPER_RATE_LIMIT"), 0.0) or None,
            api_host=os.getenv("API_HOST", "127.0.0.1"),
            api_port=_safe_int(os.getenv("API_PORT"), 8000),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            api_allowed_origins=_parse_csv(os.getenv("API_ALLOWED_ORIGINS")),
            api_trusted_hosts=_parse_csv(os.getenv("API_TRUSTED_HOSTS")),
            api_keys=_parse_csv(os.getenv("API_KEYS")),
            api_rate_limit_per_minute=_safe_int(os.getenv("API_RATE_LIMIT_PER_MINUTE"), 60),
            api_scrape_rate_limit_per_minute=_safe_int(
                os.getenv("API_SCRAPE_RATE_LIMIT_PER_MINUTE"), 60
            ),
            api_research_rate_limit_per_minute=_safe_int(
                os.getenv("API_RESEARCH_RATE_LIMIT_PER_MINUTE"), 15
            ),
            api_max_concurrent_requests=_safe_int(os.getenv("API_MAX_CONCURRENT_REQUESTS"), 10),
            api_max_request_bytes=_safe_int(os.getenv("API_MAX_REQUEST_BYTES"), 65536),
            research_max_concurrent_sources=_safe_int(
                os.getenv("RESEARCH_MAX_CONCURRENT_SOURCES"), 5
            ),
            research_timeout_per_source=_safe_float(os.getenv("RESEARCH_TIMEOUT_PER_SOURCE"), 30.0),
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            default_research_model=os.getenv(
                "DEFAULT_RESEARCH_MODEL",
                "gpt-oss:120b-cloud",
            ),
            research_normal_auto_min_sources=_safe_int(
                os.getenv("RESEARCH_NORMAL_AUTO_MIN_SOURCES"), 1
            ),
            research_normal_auto_max_sources=_safe_int(
                os.getenv("RESEARCH_NORMAL_AUTO_MAX_SOURCES"), 15
            ),
            research_deep_min_sources=_safe_int(os.getenv("RESEARCH_DEEP_MIN_SOURCES"), 15),
            research_deep_max_sources=_safe_int(os.getenv("RESEARCH_DEEP_MAX_SOURCES"), 50),
            research_default_normal_source_target=_safe_int(
                os.getenv("RESEARCH_DEFAULT_NORMAL_SOURCE_TARGET"), 5
            ),
            research_default_deep_source_target=_safe_int(
                os.getenv("RESEARCH_DEFAULT_DEEP_SOURCE_TARGET"), 20
            ),
            research_search_pool_extra_normal=_safe_int(
                os.getenv("RESEARCH_SEARCH_POOL_EXTRA_NORMAL"), 5
            ),
            research_search_pool_extra_deep=_safe_int(
                os.getenv("RESEARCH_SEARCH_POOL_EXTRA_DEEP"), 10
            ),
            research_normal_content_limit_chars=_safe_int(
                os.getenv("RESEARCH_NORMAL_CONTENT_LIMIT_CHARS"), 2500
            ),
            research_deep_content_limit_chars=_safe_int(
                os.getenv("RESEARCH_DEEP_CONTENT_LIMIT_CHARS"), 8000
            ),
            research_non_deep_source_char_cap=_safe_int(
                os.getenv("RESEARCH_NON_DEEP_SOURCE_CHAR_CAP"), 10000
            ),
            research_planning_timeout_seconds=_safe_float(
                os.getenv("RESEARCH_PLANNING_TIMEOUT_SECONDS"), 30.0
            ),
            research_source_selection_timeout_seconds=_safe_float(
                os.getenv("RESEARCH_SOURCE_SELECTION_TIMEOUT_SECONDS"), 60.0
            ),
            research_synthesis_timeout_seconds=_safe_float(
                os.getenv("RESEARCH_SYNTHESIS_TIMEOUT_SECONDS"), 120.0
            ),
            research_deep_synthesis_timeout_seconds=_safe_float(
                os.getenv("RESEARCH_DEEP_SYNTHESIS_TIMEOUT_SECONDS"), 240.0
            ),
            research_enable_query_rewrite=_parse_bool(
                os.getenv("RESEARCH_ENABLE_QUERY_REWRITE"),
                True,
            ),
            research_query_rewrite_max_variants=_safe_int(
                os.getenv("RESEARCH_QUERY_REWRITE_MAX_VARIANTS"), 4
            ),
            research_query_rewrite_timeout_seconds=_safe_float(
                os.getenv("RESEARCH_QUERY_REWRITE_TIMEOUT_SECONDS"), 20.0
            ),
            research_strict_deep_mode=_parse_bool(
                os.getenv("RESEARCH_STRICT_DEEP_MODE"),
                True,
            ),
            research_evidence_gate_enabled=_parse_bool(
                os.getenv("RESEARCH_EVIDENCE_GATE_ENABLED"),
                True,
            ),
            research_retry_aggressive_enabled=_parse_bool(
                os.getenv("RESEARCH_RETRY_AGGRESSIVE_ENABLED"),
                True,
            ),
            research_intent_router_v2=_parse_bool(
                os.getenv("RESEARCH_INTENT_ROUTER_V2"),
                True,
            ),
            research_enable_google_fallback=_parse_bool(
                os.getenv("RESEARCH_ENABLE_GOOGLE_FALLBACK"),
                True,
            ),
            research_google_fallback_min_results=_safe_int(
                os.getenv("RESEARCH_GOOGLE_FALLBACK_MIN_RESULTS"), 5
            ),
            research_rerank_domain_diversity_boost=_safe_float(
                os.getenv("RESEARCH_RERANK_DOMAIN_DIVERSITY_BOOST"), 0.25
            ),
            research_rerank_same_domain_penalty=_safe_float(
                os.getenv("RESEARCH_RERANK_SAME_DOMAIN_PENALTY"), 0.08
            ),
            research_rerank_exact_query_boost=_safe_float(
                os.getenv("RESEARCH_RERANK_EXACT_QUERY_BOOST"), 0.15
            ),
            duckduckgo_request_timeout_seconds=_safe_float(
                os.getenv("DUCKDUCKGO_REQUEST_TIMEOUT_SECONDS"), 30.0
            ),
            duckduckgo_request_delay_seconds=_safe_float(
                os.getenv("DUCKDUCKGO_REQUEST_DELAY_SECONDS"), 0.5
            ),
            google_request_timeout_seconds=_safe_float(
                os.getenv("GOOGLE_REQUEST_TIMEOUT_SECONDS"), 30.0
            ),
            flaresolverr_enabled=_parse_bool(os.getenv("FLARESOLVERR_ENABLED"), True),
            flaresolverr_url=os.getenv(
                "FLARESOLVERR_URL",
                "http://web-research-flaresolverr:8191/v1",
            ),
            flaresolverr_fallback_urls=(
                _parse_csv(os.getenv("FLARESOLVERR_FALLBACK_URLS"))
                or [
                    "http://localhost:8191/v1",
                    "http://web-research-flaresolverr-dev:8191/v1",
                ]
            ),
            flaresolverr_request_timeout_seconds=_safe_float(
                os.getenv("FLARESOLVERR_REQUEST_TIMEOUT_SECONDS"),
                45.0,
            ),
            flaresolverr_max_timeout_ms=_safe_int(
                os.getenv("FLARESOLVERR_MAX_TIMEOUT_MS"),
                60000,
            ),
            flaresolverr_max_attempts=_safe_int(
                os.getenv("FLARESOLVERR_MAX_ATTEMPTS"),
                2,
            ),
            flaresolverr_retry_backoff_seconds=_safe_float(
                os.getenv("FLARESOLVERR_RETRY_BACKOFF_SECONDS"),
                1.0,
            ),
            max_query_length=_safe_int(os.getenv("MAX_QUERY_LENGTH"), 500),
            max_source_content_chars=_safe_int(os.getenv("MAX_SOURCE_CONTENT_CHARS"), 12000),
            cache_ttl_seconds=_safe_int(os.getenv("CACHE_TTL_SECONDS"), 300),
            cache_max_entries=_safe_int(os.getenv("CACHE_MAX_ENTRIES"), 256),
            circuit_breaker_failure_threshold=_safe_int(
                os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD"), 5
            ),
            circuit_breaker_recovery_seconds=_safe_int(
                os.getenv("CIRCUIT_BREAKER_RECOVERY_SECONDS"), 30
            ),
            history_db_path=os.getenv("HISTORY_DB_PATH", "web_scraper_history.sqlite3"),
            metrics_enabled=_parse_bool(os.getenv("METRICS_ENABLED"), True),
            app_env=os.getenv("APP_ENV", "production"),
            scraper_max_raw_text_chars=_safe_int(os.getenv("SCRAPER_MAX_RAW_TEXT_CHARS"), 50000),
        )


config = Config.from_env()
