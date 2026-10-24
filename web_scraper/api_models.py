"""Pydantic models for the HTTP API."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from web_scraper.config import config
from web_scraper.web_tools.search import normalize_domains, normalize_language, normalize_region
from web_scraper.web_tools.settings import (
    EXTRACT_MAX_CHARS_CEILING,
    EXTRACT_MAX_CHARS_FLOOR,
    EXTRACT_MAX_ITEMS_CEILING,
    EXTRACT_OFFSET_CEILING,
    MIN_MARKDOWN_CHARS,
    SCRAPE_BATCH_MAX_TOTAL_CHARS,
    SCRAPE_BATCH_MAX_URLS,
    SCRAPE_BATCH_MIN_TOTAL_CHARS,
    SCRAPE_BATCH_MIN_URLS,
    SCRAPE_MAX_LINKS_CEILING,
    SEARCH_READ_CHARS_MAX,
    SEARCH_READ_MAX,
)


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
    depth: Optional[Literal["fast", "discover", "deep"]] = Field(default=None)

    region: Optional[str] = Field(
        default=None,
        description="fi-fi, us-en, wt-wt (none)",
    )
    language: Optional[str] = Field(default=None, description="Language tag: fi, en, pt-BR")
    sites: Optional[List[str]] = Field(
        default=None,
        max_length=5,
        description="Up to 5 domains: only their results",
    )
    exclude_sites: Optional[List[str]] = Field(
        default=None, max_length=5, description="Up to 5 domains kept out"
    )
    date_from: Optional[date] = Field(
        default=None,
        description="ISO date: earlier results are dropped, undated stay",
    )
    date_to: Optional[date] = Field(default=None, description="ISO date: later results dropped")
    read: int = Field(
        default=0,
        ge=0,
        le=SEARCH_READ_MAX,
        description="Also read the top N results (costs seconds)",
    )
    read_chars: Optional[int] = Field(
        default=None,
        ge=MIN_MARKDOWN_CHARS,
        le=SEARCH_READ_CHARS_MAX,
        description="With read: Markdown characters per page (4,000)",
    )
    archive: bool = Field(
        default=False,
        description="With read: use the Internet Archive for walled pages",
    )
    resolve_dates: Optional[bool] = Field(
        default=None,
        description="Read the first 64 KB of the top pages for dates (default: news, date ranges)",
    )
    strict_dates: bool = Field(
        default=False,
        description="Drop results with no date or a low date_confidence",
    )
    compact: Optional[bool] = Field(
        default=None,
        description="Compact answer (the default here); false returns everything",
    )
    debug: bool = Field(
        default=False,
        description="With compact: add facets, queries, timings, score parts",
    )

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        return value.strip()

    @field_validator("region")
    @classmethod
    def check_region(cls, value: Optional[str]) -> Optional[str]:
        return normalize_region(value) if value is not None else None

    @field_validator("language")
    @classmethod
    def check_language(cls, value: Optional[str]) -> Optional[str]:
        return normalize_language(value) if value is not None else None

    @field_validator("sites")
    @classmethod
    def check_sites(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return normalize_domains(value, "sites") or None

    @field_validator("exclude_sites")
    @classmethod
    def check_exclude_sites(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return normalize_domains(value, "exclude_sites") or None

    @model_validator(mode="after")
    def check_range(self) -> WebSearchRequest:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self


def _describe_read_fields(schema: Dict[str, Any]) -> None:
    """The fields ``read`` adds to the results it reads, only there: not declared, so a result
    that was not read carries no empty ones."""
    schema.setdefault("properties", {}).update(
        {
            "content": {"type": "string", "description": "With read: the page's Markdown"},
            "content_truncated": {
                "type": "boolean",
                "description": "With read: the page has more than content holds",
            },
            "content_error": {
                "type": "object",
                "description": "With read: why the page was not read (code, message, hint)",
            },
        }
    )


class WebSearchResult(BaseModel):
    model_config = ConfigDict(extra="allow", json_schema_extra=_describe_read_fields)

    title: str
    url: str
    snippet: str = ""
    source: str = ""
    domain: str = ""
    source_type: str = Field(
        default="other",
        description=(
            "official, academic, news (an established outlet), press_release, social, "
            "reference, code, community (forums, blog hosts) or other"
        ),
    )
    year: Optional[int] = None
    recency: str = Field(
        default="unknown",
        description="By the newest year the result names: current is this year or last",
    )
    date: Optional[str] = Field(
        default=None,
        description="YYYY-MM-DD or YYYY-MM read from the address, the snippet or the title",
    )
    date_source: Optional[str] = Field(
        default=None, description="url, snippet, title, provider, or the page's own signal"
    )
    date_confidence: float = Field(
        default=0.0,
        description="0 to 1: agreeing signals raise it, an address-only or snippet-only date is low",
    )
    date_conflicts: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Signals more than a day from date, each with source and value",
    )
    date_modified: Optional[str] = None
    age_days: Optional[int] = None
    freshness: str = Field(
        default="undated",
        description="this_week, this_month, this_year, older, or undated, by the date first",
    )
    kind: str = Field(
        default="page",
        description="article, listing (a section, tag or archive), front (a home page), or page",
    )
    angles: List[str] = Field(default_factory=list)
    relevance: float = 0.0
    bm25: float = 0.0
    score: float = 0.0


class WebSearchQuery(BaseModel):
    angle: str
    query: str
    date_filter: Optional[str] = None
    provider: Optional[str] = None
    results: int = 0
    new: int = 0
    error: Optional[Dict[str, Any]] = None
    fallback: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Set when a fallback provider was asked: from, to, reason, error, and tried "
            "(each provider asked or skipped, in order, with its status and reason)"
        ),
    )


class WebSearchFacets(BaseModel):
    source_types: Dict[str, int] = Field(default_factory=dict)
    recency: Dict[str, int] = Field(default_factory=dict)
    freshness: Dict[str, int] = Field(default_factory=dict)
    kinds: Dict[str, int] = Field(default_factory=dict)
    domains: Dict[str, int] = Field(default_factory=dict)
    angles: Dict[str, int] = Field(default_factory=dict)
    total_candidates: int = 0
    domain_cap: int = 0


class WebSearchResponse(BaseModel):
    query: str
    provider: str = Field(..., description="Provider(s) that answered, joined with + when mixed")
    requested_provider: Optional[str] = None
    depth: str = "discover"
    results: List[WebSearchResult]
    facets: WebSearchFacets = Field(default_factory=WebSearchFacets)
    queries: List[WebSearchQuery] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    cache_hit: bool = False
    # On a cache hit: the timings of the call that filled the cache, and when.
    cached_timings: Optional[Dict[str, float]] = None
    cached_at: Optional[str] = None
    dates: Optional[Dict[str, Any]] = Field(
        default=None,
        description="With resolve_dates: pages asked, read, resolved, failed, cut, and the budget",
    )
    timings: Dict[str, float]


def _flatten_scrape_schema(schema: Dict[str, Any]) -> None:
    """Show ``url`` and ``urls`` as what they are, not as nullable unions.

    Neither is required alone (one of the two is), so pydantic writes both as a union with
    null; a model reading the tool's schema wants a string and an array. The schema has no
    top-level ``oneOf``, which several providers refuse in a tool's input schema.
    """
    properties = schema.get("properties", {})
    for name in ("url", "urls", "content_id"):
        union = properties.get(name)
        if not union or "anyOf" not in union:
            continue
        kept = next(option for option in union["anyOf"] if option.get("type") != "null")
        flat = {key: value for key, value in kept.items() if key != "title"}
        if "description" in union:
            flat["description"] = union["description"]
        properties[name] = flat


class WebScrapeRequest(BaseModel):
    """Single-page scrape request, or several pages in one call with ``urls``."""

    model_config = ConfigDict(json_schema_extra=_flatten_scrape_schema)

    url: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=2048,
        description="The page; one of url, urls, content_id",
    )
    urls: Optional[List[str]] = Field(
        default=None,
        min_length=SCRAPE_BATCH_MIN_URLS,
        max_length=SCRAPE_BATCH_MAX_URLS,
        description="2 to 10 pages in one call",
    )
    max_total_chars: Optional[int] = Field(
        default=None,
        ge=SCRAPE_BATCH_MIN_TOTAL_CHARS,
        le=SCRAPE_BATCH_MAX_TOTAL_CHARS,
        description="With urls: cap on the whole answer (60,000)",
    )
    max_chars: Optional[int] = Field(
        default=None,
        ge=MIN_MARKDOWN_CHARS,
        le=200_000,
        description=(
            f"Markdown cap, at least {MIN_MARKDOWN_CHARS} (the server's own, 50,000, applies "
            "above); bounds the Markdown only"
        ),
    )
    links: bool = Field(
        default=True,
        description="false returns no links",
    )
    max_links: Optional[int] = Field(
        default=None,
        ge=0,
        le=SCRAPE_MAX_LINKS_CEILING,
        description="Most links returned, 0 to 200 (50)",
    )
    archive: bool = Field(
        default=False,
        description="Read the Internet Archive's copy of a walled page; alone, only that",
    )
    recover: Optional[bool] = Field(
        default=None,
        description="A walled page: false does not look for it elsewhere",
    )
    lean: Optional[bool] = Field(
        default=None,
        description="Only text, date and cut info; a content_id read is lean unless false",
    )
    content_id: Optional[str] = Field(
        default=None,
        pattern=r"^c-[0-9a-f]{12}$",
        description="Instead of url: read on in a page a scrape cut (cache, 5 minutes)",
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=100_000_000,
        description="With content_id: where to start (next_offset)",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: Optional[str]) -> Optional[str]:
        return normalize_url(value) if value is not None else None

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, values: Optional[List[str]]) -> Optional[List[str]]:
        if values is None:
            return None
        for value in values:
            if not 3 <= len(value.strip()) <= 2048:
                raise ValueError("each URL must be 3 to 2048 characters")
        return [normalize_url(value) for value in values]

    def service_options(self) -> Dict[str, Any]:
        """The optional arguments the service's ``scrape`` takes, only those the request sets.

        A lean answer has no links unless the request asked for some.
        """
        options = self.model_dump(include={"links", "max_links", "recover"}, exclude_defaults=True)
        if self.lean:
            options["lean"] = True
            if not {"links", "max_links"} & self.model_fields_set:
                options["links"] = False
        return options

    @model_validator(mode="after")
    def exactly_one_of_url_urls_and_content_id(self) -> WebScrapeRequest:
        given = [value is not None for value in (self.url, self.urls, self.content_id)]
        if sum(given) != 1:
            raise ValueError("give exactly one of url, urls and content_id")
        if self.offset and self.content_id is None:
            raise ValueError("offset goes with content_id")
        return self


class WebLink(BaseModel):
    url: str
    text: str = ""
    kind: str = ""


class WebArchiveSkipped(BaseModel):
    timestamp: str = Field(description="When the Internet Archive took the copy, UTC")
    reason: str = Field(description="Why it was not read: captcha, access_wall, empty_content, ...")


class WebScrapeResponse(BaseModel):
    url: str
    requested_url: str = ""
    title: str
    description: str = ""
    canonical_url: str = ""
    markdown: str
    status_code: int = 0
    content_type: str = ""
    links: List[WebLink] = Field(default_factory=list)
    truncated: bool = Field(
        default=False,
        description="True when max_chars (or offset and max_chars) left the rest of the page out",
    )
    total_chars: Optional[int] = Field(
        default=None, description="The length of the page's whole Markdown"
    )
    offset: Optional[int] = Field(
        default=None, description="Where this part starts, with content_id"
    )
    next_offset: Optional[int] = Field(
        default=None, description="With truncated: where the next part starts"
    )
    content_id: Optional[str] = Field(
        default=None,
        description="With truncated: ask with it and next_offset for the rest, no request is made",
    )
    links_truncated: bool = Field(
        default=False,
        description="True when the page has links that links leaves out (page chrome or cap)",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)
    metadata_truncated: bool = Field(
        default=False,
        description="True when the page's meta tags or JSON-LD were cut to 10,000 characters",
    )
    content_hash: str = ""
    date: Optional[str] = Field(
        default=None,
        description="The page's published date, YYYY-MM-DD, from all its date signals",
    )
    date_source: Optional[str] = Field(
        default=None,
        description="Which signal gave date (json_ld:datePublished, meta:..., time, ...)",
    )
    date_confidence: float = Field(
        default=0.0,
        description="0 to 1: agreeing signals raise it, a lone address or printed date is low",
    )
    date_conflicts: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Signals more than a day from date, each with source and value",
    )
    date_modified: Optional[str] = Field(
        default=None, description="When the page says it was updated"
    )
    fetched_with: str = "http"
    cache_hit: bool = False
    # On a cache hit: the timings of the call that filled the cache, and when.
    cached_timings: Optional[Dict[str, float]] = None
    cached_at: Optional[str] = None
    archived_at: Optional[str] = Field(
        default=None, description="When the Internet Archive took the copy read with archive"
    )
    archive_url: Optional[str] = None
    archive_skipped: Optional[List[WebArchiveSkipped]] = Field(
        default=None,
        description=(
            "With archive, the newer copies that were not read (a captcha or a login wall the "
            "archive's crawler met, an empty page), newest first"
        ),
    )
    recovered_from: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Set when the page was walled and read another way: kind (archive, amp, print, feed, "
            "other_source) and url of where the text came from; the text begins with a line "
            "saying so. For other_source, url is that outlet's, not the page's"
        ),
    )
    alternates: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="With recovered_from: other addresses (url, domain, title, date, source_type, why)",
    )
    next_action: Optional[str] = Field(
        default=None, description="With recovered_from: one sentence on what to do with the text"
    )
    warnings: List[str] = Field(default_factory=list)
    timings: Dict[str, float] = Field(default_factory=dict)
    pages: Optional[int] = Field(default=None, description="Page count of a PDF document")


class WebScrapeBatchPage(WebScrapeResponse):
    """A page of a batch: its answer, and whether the batch's cap cut its Markdown."""

    markdown_truncated: bool = Field(
        default=False, description="True when max_total_chars cut this page's Markdown"
    )


class WebScrapeBatchError(BaseModel):
    """A page of a batch that failed: the URL as given and the error envelope."""

    url: str
    error: Dict[str, Any]


class WebScrapeBatchResponse(BaseModel):
    pages: List[Union[WebScrapeBatchPage, WebScrapeBatchError]]
    timings: Dict[str, float]


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
    # Every URL that matched, before the limit; ``returned`` counts ``urls``.
    total: int
    returned: int = 0
    truncated: bool = False
    entries: List[WebMapEntry]
    sitemaps: List[str]
    errors: List[Dict[str, Any]]


class WebExtractField(BaseModel):
    """How to find one field; with no selector it is read from the page's structured data."""

    model_config = ConfigDict(extra="allow")

    selector: Optional[str] = Field(
        default=None,
        description=(
            "CSS selector. With none (or one that finds nothing, for a well-known name), the field "
            "is read by its name from JSON-LD, meta tags and HTML conventions: headline, title, "
            "author, published_at, modified_at, description, image, site_name, section, keywords, "
            "language, url, lead (the first paragraph), body, word_count, name, price"
        ),
    )
    fallback_selectors: List[str] = Field(
        default_factory=list,
        description="Selectors tried in order when selector finds nothing",
    )
    attribute: Optional[str] = Field(
        default=None,
        description="Read this attribute (href, src, content, datetime) instead of the text",
    )
    all: bool = Field(default=False, description="Every match as a list, not only the first")
    max_items: Optional[int] = Field(
        default=None,
        ge=1,
        le=EXTRACT_MAX_ITEMS_CEILING,
        description=(
            "With all: the most values returned (100 by default); the field's provenance "
            "holds total, returned, and truncated"
        ),
    )
    offset: Optional[int] = Field(
        default=None,
        ge=0,
        le=EXTRACT_OFFSET_CEILING,
        description="With all: skip the first N matches; provenance has next_offset to page on",
    )
    type: str = Field(
        default="string",
        description="string, number, integer, boolean, or date (ISO 8601; dates by name are)",
    )
    required: bool = Field(
        default=False,
        description="Report required_field_missing in errors when nothing is found",
    )


class WebExtractRequest(BaseModel):
    """Structured extraction request using CSS selectors."""

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(..., min_length=3, max_length=2048)
    extraction_schema: Dict[str, Union[str, WebExtractField]] = Field(
        ...,
        alias="schema",
        min_length=1,
        description="Field name to a CSS selector string or a field object",
    )
    max_chars: Optional[int] = Field(
        default=None,
        ge=EXTRACT_MAX_CHARS_FLOOR,
        le=EXTRACT_MAX_CHARS_CEILING,
        description=(
            "Cap on the whole answer in characters (20,000 by default); values and evidence "
            "are cut to fit, and truncated says so"
        ),
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_url(value)

    def field_specs(self) -> Dict[str, Any]:
        """The schema as the service reads it: a selector string or a plain object per field."""
        return {
            name: spec if isinstance(spec, str) else spec.model_dump(exclude_none=True)
            for name, spec in self.extraction_schema.items()
        }


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
    date: Optional[str] = Field(
        default=None,
        description="The page's published date, YYYY-MM-DD, from all its date signals",
    )
    date_source: Optional[str] = Field(
        default=None,
        description="Which signal gave date (json_ld:datePublished, meta:..., time, ...)",
    )
    date_confidence: float = Field(
        default=0.0,
        description="0 to 1: agreeing signals raise it, a lone address or printed date is low",
    )
    date_conflicts: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Signals more than a day from date, each with source and value",
    )
    date_modified: Optional[str] = Field(
        default=None, description="When the page says it was updated"
    )
    fetched_with: str
    timings: Dict[str, float]
    cache_hit: bool = Field(
        default=False,
        description="True when the page was read from the short-lived page cache, not fetched",
    )
    cached_timings: Optional[Dict[str, float]] = Field(
        default=None, description="On a cache hit: what fetching the page cost when it was fetched"
    )
    warnings: List[str] = Field(default_factory=list)
    truncated: bool = Field(
        default=False,
        description="True when max_items or max_chars left values out or cut a text",
    )


class WebCrawlOptions(BaseModel):
    """Options for a background crawl job."""

    max_pages: Optional[int] = Field(default=None, ge=1, le=10_000)
    max_depth: Optional[int] = Field(default=None, ge=0, le=10)
    include_subdomains: bool = False
    include_patterns: List[str] = Field(default_factory=list, max_length=50)
    exclude_patterns: List[str] = Field(default_factory=list, max_length=50)
    respect_robots: Optional[bool] = None
    use_sitemap: Optional[bool] = Field(
        default=None,
        description="Also start from the site's sitemaps (pages no link reaches); counts as pages",
    )


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
