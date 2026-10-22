"""Pydantic models for the HTTP API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from web_scraper.config import config


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    """Accept bare hostnames and normalize them into valid HTTP URLs."""
    normalized = url.strip()
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized}"
    return normalized


class ScrapeRequest(BaseModel):
    """Single page scraping request."""

    url: str = Field(..., min_length=3, max_length=2048)
    timeout: Optional[float] = Field(default=None, ge=1, le=120)
    max_links: Optional[int] = Field(default=None, ge=1, le=500)
    include_metadata: bool = True
    include_links: bool = True
    include_images: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_url(value)


class BatchScrapeRequest(BaseModel):
    """Batch scraping request."""

    urls: List[str] = Field(..., min_length=1, max_length=100)
    timeout: Optional[float] = Field(default=None, ge=1, le=120)
    max_links: Optional[int] = Field(default=None, ge=1, le=500)
    max_concurrent: Optional[int] = Field(default=None, ge=1, le=20)

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, values: List[str]) -> List[str]:
        return [normalize_url(value) for value in values]


class ResearchToolRequest(BaseModel):
    """Input schema for the external web research tool."""

    query: str = Field(..., min_length=3, max_length=config.max_query_length)
    max_sources: Optional[int] = Field(default=None, ge=1, le=50)
    deep_mode: bool = False
    model: Optional[str] = Field(default=None, min_length=1, max_length=128)
    include_source_content: bool = False
    provider: Literal["ollama", "openai"] = "ollama"
    research_profile: Literal["technical", "news", "academic", "auto"] = "auto"
    openai_api_key: Optional[str] = Field(default=None, max_length=512)
    ollama_api_key: Optional[str] = Field(default=None, max_length=512)
    ollama_base_url: Optional[str] = Field(default=None, max_length=512)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        return value.strip()


class ResearchCitation(BaseModel):
    """Machine-friendly citation block for downstream LLMs."""

    source: str
    url: str
    title: str
    relevance_score: float = 0.0
    snippet: str = ""
    source_tier: int = 5  # 1 (official) … 5 (blog/unknown)
    publication_date: Optional[str] = None


class ResearchSource(BaseModel):
    """Detailed source payload."""

    source: str
    url: str
    title: str
    content: Optional[str] = None
    relevance_score: float = 0.0
    error: Optional[str] = None
    source_tier: int = 5
    publication_date: Optional[str] = None


class DataTableRow(BaseModel):
    """One row of the numeric data table in a high-reliability research response."""

    metric: str
    value: str
    source: str
    date: str = "unknown"


class ResearchMetadata(BaseModel):
    """Operational metadata for a research response."""

    model: str
    generated_at: str = Field(default_factory=utc_now_iso)
    sources_checked: int
    sources_succeeded: int
    cached: bool = False
    trace_id: str
    response_ms: float
    query_hash: str = ""


def _coerce_str(v: Any) -> str:
    """Coerce a value to str — joins lists with newlines, passes strings through."""
    if isinstance(v, list):
        return "\n".join(str(item) for item in v)
    return v or ""


class WebResearchResponse(BaseModel):
    """Stable response contract for LLM-facing research calls."""

    query: str
    answer: str
    summary: str
    key_findings: List[str]
    detailed_analysis: str = ""
    recommendations: str = ""
    # FAZ 6 — high-reliability structured output
    executive_summary: str = ""
    data_table: List[DataTableRow] = Field(default_factory=list)
    conflicts_uncertainty: List[str] = Field(default_factory=list)
    confidence_level: str = "Medium"
    confidence_reason: str = ""
    citations: List[ResearchCitation]
    sources: List[ResearchSource]
    metadata: ResearchMetadata

    @field_validator(
        "recommendations",
        "detailed_analysis",
        "executive_summary",
        "confidence_reason",
        "summary",
        "answer",
        mode="before",
    )
    @classmethod
    def coerce_str_fields(cls, v: Any) -> Any:
        # LLM occasionally returns a list instead of a string for text fields.
        if isinstance(v, list):
            return "\n".join(str(item) for item in v)
        return v


class LegacyResearchResponse(BaseModel):
    """Backward-compatible response used by the current UI."""

    query: str
    summary: str
    key_findings: List[str]
    detailed_analysis: str = ""
    recommendations: str = ""
    # FAZ 6 — high-reliability structured output
    executive_summary: str = ""
    data_table: List[DataTableRow] = Field(default_factory=list)
    conflicts_uncertainty: List[str] = Field(default_factory=list)
    confidence_level: str = "Medium"
    confidence_reason: str = ""
    sources: List[ResearchSource]
    sources_checked: int
    sources_succeeded: int

    @field_validator(
        "recommendations",
        "detailed_analysis",
        "executive_summary",
        "confidence_reason",
        "summary",
        mode="before",
    )
    @classmethod
    def coerce_str_fields(cls, v: Any) -> Any:
        # LLM occasionally returns a list instead of a string for text fields.
        if isinstance(v, list):
            return "\n".join(str(item) for item in v)
        return v


class ToolDescriptor(BaseModel):
    """Tool manifest entry."""

    name: str
    description: str
    method: str
    path: str
    stream_path: Optional[str] = None
    auth: Dict[str, Any]
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    example: Dict[str, Any]


class ToolsManifest(BaseModel):
    """Manifest consumed by client frameworks that need discoverability."""

    tools: List[ToolDescriptor]


class WebSearchRequest(BaseModel):
    """URL discovery request backed by a replaceable search adapter."""

    query: str = Field(..., min_length=1, max_length=10_000)
    limit: Optional[int] = Field(default=None, ge=1, le=50)
    provider: Optional[str] = Field(default=None, min_length=1, max_length=32)
    depth: Optional[Literal["fast", "discover", "deep"]] = Field(
        default=None,
        description=(
            "fast sends one query; discover adds current-year and historical angles; "
            "deep adds an official or primary-source angle"
        ),
    )

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        return value.strip()


class WebSearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    domain: str = ""
    source_type: str = "other"
    year: Optional[int] = None
    recency: str = "unknown"
    angles: List[str] = Field(default_factory=list)
    relevance: float = 0.0
    score: float = 0.0


class WebSearchQuery(BaseModel):
    angle: str
    query: str
    results: int = 0
    new: int = 0
    error: Optional[Dict[str, Any]] = None


class WebSearchFacets(BaseModel):
    source_types: Dict[str, int] = Field(default_factory=dict)
    recency: Dict[str, int] = Field(default_factory=dict)
    domains: Dict[str, int] = Field(default_factory=dict)
    angles: Dict[str, int] = Field(default_factory=dict)
    total_candidates: int = 0
    domain_cap: int = 0


class WebSearchResponse(BaseModel):
    query: str
    provider: str
    depth: str = "discover"
    results: List[WebSearchResult]
    facets: WebSearchFacets = Field(default_factory=WebSearchFacets)
    queries: List[WebSearchQuery] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    timings: Dict[str, float]


class WebScrapeRequest(BaseModel):
    """Single-page scrape request."""

    url: str = Field(..., min_length=3, max_length=2048)
    max_chars: Optional[int] = Field(default=None, ge=1, le=200_000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_url(value)


class WebLink(BaseModel):
    url: str
    text: str = ""
    kind: str = ""


class WebScrapeResponse(BaseModel):
    url: str
    requested_url: str
    title: str
    description: str
    canonical_url: str
    markdown: str
    status_code: int
    content_type: str
    links: List[WebLink]
    metadata: Dict[str, Any]
    content_hash: str
    fetched_with: str
    cache_hit: bool = False
    warnings: List[str] = Field(default_factory=list)
    timings: Dict[str, float]
    pages: Optional[int] = Field(default=None, description="Page count of a PDF document")


class WebMapRequest(BaseModel):
    """URL map request covering sitemaps and page links."""

    url: str = Field(..., min_length=3, max_length=2048)
    include_subdomains: bool = False
    search: Optional[str] = Field(default=None, max_length=256)
    include_patterns: List[str] = Field(default_factory=list, max_length=50)
    exclude_patterns: List[str] = Field(default_factory=list, max_length=50)
    limit: Optional[int] = Field(default=None, ge=1, le=100_000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_url(value)


class WebMapEntry(BaseModel):
    url: str
    lastmod: Optional[str] = None
    source: str = "links"


class WebMapResponse(BaseModel):
    url: str
    urls: List[str]
    total: int
    entries: List[WebMapEntry]
    sitemaps: List[str]
    errors: List[Dict[str, Any]]


class WebExtractRequest(BaseModel):
    """Structured extraction request using CSS selectors."""

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(..., min_length=3, max_length=2048)
    extraction_schema: Dict[str, Any] = Field(..., alias="schema", min_length=1)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_url(value)


class WebExtractResponse(BaseModel):
    url: str
    requested_url: str
    title: str
    description: str
    canonical_url: str
    status_code: int
    data: Dict[str, Any]
    provenance: Dict[str, Any]
    errors: List[Dict[str, Any]]
    fetched_with: str
    timings: Dict[str, float]
    warnings: List[str] = Field(default_factory=list)


class WebCrawlOptions(BaseModel):
    """Options for a background crawl job."""

    max_pages: Optional[int] = Field(default=None, ge=1, le=10_000)
    max_depth: Optional[int] = Field(default=None, ge=0, le=10)
    include_subdomains: bool = False
    include_patterns: List[str] = Field(default_factory=list, max_length=50)
    exclude_patterns: List[str] = Field(default_factory=list, max_length=50)
    respect_robots: Optional[bool] = None


class WebCrawlRequest(BaseModel):
    url: str = Field(..., min_length=3, max_length=2048)
    options: Optional[WebCrawlOptions] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_url(value)


class WebCrawlJobResponse(BaseModel):
    job_id: str
    url: str
    status: str
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    options: Dict[str, Any] = Field(default_factory=dict)
    progress: Dict[str, Any] = Field(default_factory=dict)
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    duplicates: List[Dict[str, Any]] = Field(default_factory=list)


class WebCrawlResultsResponse(BaseModel):
    job_id: str
    status: str
    total: int
    offset: int
    limit: int
    results: List[Dict[str, Any]]
    errors: List[Dict[str, Any]]
    duplicates: List[Dict[str, Any]]
