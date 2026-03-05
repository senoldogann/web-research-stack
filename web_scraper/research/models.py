"""Data models for the research agent."""

from dataclasses import dataclass, field
from typing import Dict, Optional, Union


@dataclass
class ResearchResult:
    """Result from a single source."""

    source: str
    url: str
    title: str
    content: str
    relevance_score: float = 0.0
    error: Optional[str] = None
    # FAZ 6 — high-reliability metadata
    source_tier: int = 5  # 1 (official/primary) … 5 (blog/unknown)
    publication_date: Optional[str] = None  # ISO date extracted from page


@dataclass
class ResearchReport:
    """Comprehensive research report."""

    query: str
    sources: list[ResearchResult] = field(default_factory=list)
    summary: str = ""
    key_findings: list[str] = field(default_factory=list)
    detailed_analysis: str = ""
    recommendations: str = ""
    sources_checked: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    # FAZ 6 — high-reliability structured output
    executive_summary: str = ""
    data_table: list[dict] = field(default_factory=list)
    conflicts_uncertainty: list[str] = field(default_factory=list)
    confidence_level: str = "Medium"  # High / Medium / Low
    confidence_reason: str = ""
    # Mode/intent/retrieval telemetry (backward-compatible, optional)
    intent_class: Optional[str] = None
    execution_mode_requested: Optional[str] = None
    execution_mode_effective: Optional[str] = None
    authority_tier_counts: Dict[str, int] = field(default_factory=dict)
    freshness_summary: Dict[str, Optional[Union[int, str]]] = field(default_factory=dict)
    retrieval_attempts: int = 1
    evidence_gate_passed: Optional[bool] = None
    extended_analysis_hidden: bool = False
