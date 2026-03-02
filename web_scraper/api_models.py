"""Pydantic models for the HTTP API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

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
    research_profile: Literal["technical", "news", "academic"] = "technical"
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
