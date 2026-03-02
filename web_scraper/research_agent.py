"""Backward-compatibility shim.

All symbols have moved to ``web_scraper.research``.
This module re-exports them so existing imports keep working::

    from web_scraper.research_agent import ResearchAgent
    from web_scraper.research_agent import ResearchResult, ResearchReport
"""

from web_scraper.research.agent import ResearchAgent
from web_scraper.research.models import ResearchReport, ResearchResult

__all__ = ["ResearchAgent", "ResearchResult", "ResearchReport"]
