"""AI Agent for natural language web scraping commands using Ollama."""

import json
import re
from typing import Optional

import httpx

from web_scraper.config import config


class OllamaAgent:
    """AI agent that processes natural language commands for web scraping."""

    def __init__(self, model: Optional[str] = None, host: Optional[str] = None):
        """
        Initialize the Ollama agent.

        Args:
            model: Ollama model name (default: gpt-oss:120b-cloud)
            host: Ollama API host (default: http://localhost:11434)
        """
        self.model = model or config.default_research_model
        self.host = host or config.ollama_host
        self.api_url = f"{self.host}/api/generate"

    async def process_command(self, command: str) -> dict:
        """
        Process a natural language command and return scraping parameters.

        Args:
            command: Natural language command (e.g., "scrape anthropic site and get me the last 10 news")

        Returns:
            Dictionary with URL, scraping mode, and filters
        """
        prompt = self._create_prompt(command)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.api_url,
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                    },
                    timeout=config.research_source_selection_timeout_seconds,
                )
            response.raise_for_status()

            result = response.json()
            ai_response = result.get("response", "")

            # Parse the AI response
            return self._parse_response(ai_response, command)

        except Exception:
            # Fallback to manual parsing if AI fails
            return self._fallback_parse(command)

    def _create_prompt(self, command: str) -> str:
        """Create a prompt for the AI model."""
        return f"""You are a web scraping assistant. Extract the following information from the user's command and return ONLY a JSON object:

User command: "{command}"

Extract:
1. url: The website URL to scrape (add https:// if missing)
2. mode: "playwright" if JavaScript-heavy site (like React/Vue/Angular) or protected by Cloudflare, otherwise "basic"
3. max_items: Number of items to extract (news, articles, etc.) if specified
4. filters: Any specific content to look for (news, blog posts, products, etc.)

Return ONLY this JSON format:
{{
    "url": "https://example.com",
    "mode": "basic",
    "max_items": 10,
    "filters": "news"
}}

If URL cannot be determined, use empty string. If max_items not specified, use 0 (all)."""

    def _parse_response(self, response: str, original_command: str) -> dict:
        """Parse the AI response into a structured dictionary."""
        try:
            # Try to extract JSON from the response
            json_match = re.search(r"\{[^}]*\}", response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return {
                    "url": data.get("url", ""),
                    "mode": data.get("mode", "basic"),
                    "max_items": data.get("max_items", 0),
                    "filters": data.get("filters", ""),
                    "original_command": original_command,
                }
        except json.JSONDecodeError:
            pass

        return self._fallback_parse(original_command)

    def _fallback_parse(self, command: str) -> dict:
        """Fallback parser when AI is not available."""
        # Extract URL from command
        url_pattern = r"(https?://[^\s]+|www\.[^\s]+|[a-zA-Z0-9-]+\.(com|org|net|io|dev|co)[^\s]*)"
        url_match = re.search(url_pattern, command)

        url = ""
        if url_match:
            url = url_match.group(1)
            if not url.startswith("http"):
                url = f"https://{url}"

        # Extract number
        number_match = re.search(r"(\d+)\s*(news|articles|posts|items)?", command, re.IGNORECASE)
        max_items = int(number_match.group(1)) if number_match else 0

        # Determine if playwright needed
        mode = (
            "playwright"
            if any(
                word in command.lower() for word in ["javascript", "react", "dynamic", "cloudflare"]
            )
            else "basic"
        )

        # Extract filters
        filters = ""
        if "news" in command.lower():
            filters = "news"
        elif "blog" in command.lower():
            filters = "blog"
        elif "article" in command.lower():
            filters = "articles"

        return {
            "url": url,
            "mode": mode,
            "max_items": max_items,
            "filters": filters,
            "original_command": command,
        }

    async def summarize_content(self, content: str, filters: str = "", max_items: int = 0) -> str:
        """
        Summarize scraped content using AI.

        Args:
            content: The scraped content
            filters: Type of content to focus on
            max_items: Maximum number of items to extract

        Returns:
            Formatted summary
        """
        # Truncate content if too long
        if len(content) > config.research_deep_content_limit_chars:
            content = content[: config.research_deep_content_limit_chars] + "..."

        prompt = f"""Analyze this website content and extract the key information{f" about {filters}" if filters else ""}.

{f"Extract up to {max_items} items if applicable." if max_items > 0 else ""}

Content:
{content}

Format your response as a clean, structured output with:
1. Main topics/sections found
2. Key information points
3. Any lists or enumerated items

Keep it concise and well-organized."""

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.api_url,
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                    },
                    timeout=config.research_synthesis_timeout_seconds,
                )
            response.raise_for_status()

            result = response.json()
            return result.get("response", "Could not generate summary.")

        except Exception as e:
            return f"Error generating summary: {str(e)}"

    def is_available(self) -> bool:
        """Check if Ollama is running. Stays sync — called before the event loop."""
        try:
            response = httpx.get(
                f"{self.host}/api/tags",
                timeout=min(5.0, config.research_planning_timeout_seconds),
            )
            return response.status_code == 200
        except Exception:
            return False
