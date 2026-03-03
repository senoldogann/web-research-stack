"""LLM abstraction layer: Ollama and OpenAI HTTP callers.

``LLMClient`` is a base class that ``ResearchAgent`` inherits.
It deliberately contains *only* the transport layer so that the higher-level
research orchestration in ``agent.py`` stays clean and testable.
"""

from __future__ import annotations

from typing import Optional

import httpx

from web_scraper.config import config


class LLMClient:
    """Thin wrapper around Ollama / OpenAI chat APIs.

    Keeps a single long-lived ``httpx.AsyncClient`` to avoid TCP/TLS
    handshake overhead on every LLM call.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        host: Optional[str] = None,
        max_concurrent: Optional[int] = None,
        timeout_per_source: Optional[float] = None,
        provider: str = "ollama",
        openai_api_key: Optional[str] = None,
        ollama_api_key: Optional[str] = None,
    ) -> None:
        """Initialise the LLM transport.

        Args:
            model: LLM model name (Ollama or OpenAI).
            host: Ollama API host.  Ignored when *provider* is ``"openai"``.
            max_concurrent: Maximum concurrent scraping (passed through to agent).
            timeout_per_source: Per-source timeout in seconds (passed through to agent).
            provider: ``"ollama"`` or ``"openai"``.
            openai_api_key: Required when *provider* is ``"openai"``.
            ollama_api_key: Optional Bearer token for authenticated Ollama endpoints (cloud/self-hosted with auth).
        """
        self.provider = provider
        self.openai_api_key = openai_api_key
        self.ollama_api_key = ollama_api_key
        self.model = model or config.default_research_model
        self.host = host or config.ollama_host
        self.api_url = f"{self.host}/api/generate"
        self.max_concurrent = max_concurrent or config.research_max_concurrent_sources
        self.timeout_per_source = timeout_per_source or config.research_timeout_per_source
        self._http_client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # HTTP client lifecycle
    # ------------------------------------------------------------------

    def _get_http_client(self) -> httpx.AsyncClient:
        """Return the shared ``httpx.AsyncClient``, creating it on first use."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient()
        return self._http_client

    async def _close_http_client(self) -> None:
        """Close the shared ``httpx.AsyncClient`` if open."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    # ------------------------------------------------------------------
    # LLM dispatcher
    # ------------------------------------------------------------------

    async def _call_llm(
        self,
        prompt: str,
        timeout: float,
        max_output_tokens: int = 2048,
    ) -> str:
        """Dispatch to the configured provider and return the raw text response."""
        logger.debug(f"[DEBUG] _call_llm prompt preview: {prompt[:500]}...")
        if self.provider == "openai":
            return await self._call_openai(prompt, timeout, max_output_tokens)
        return await self._call_ollama(prompt, timeout, max_output_tokens)

    async def _call_ollama(
        self,
        prompt: str,
        timeout: float,
        max_output_tokens: int = 2048,
    ) -> str:
        """Call ``/api/generate`` on the Ollama server (local or cloud)."""
        client = self._get_http_client()
        headers: dict[str, str] = {}
        if self.ollama_api_key:
            headers["Authorization"] = f"Bearer {self.ollama_api_key}"
        response = await client.post(
            self.api_url,
            headers=headers,
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_output_tokens},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json().get("response", "")

    async def _call_openai(
        self,
        prompt: str,
        timeout: float,
        max_output_tokens: int = 2048,
    ) -> str:
        """Call the OpenAI chat completions endpoint."""
        if not self.openai_api_key:
            raise ValueError("OpenAI API key is required but not set")
        client = self._get_http_client()
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": max_output_tokens,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
