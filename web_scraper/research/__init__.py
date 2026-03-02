"""``web_scraper.research`` — modular research pipeline package.

Public API::

    from web_scraper.research import ResearchAgent, ResearchResult, ResearchReport
"""

from web_scraper.research.agent import ResearchAgent
from web_scraper.research.models import ResearchReport, ResearchResult

__all__ = ["ResearchAgent", "ResearchResult", "ResearchReport"]
