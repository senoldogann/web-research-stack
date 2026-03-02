"""Command-line interface for the web scraper."""

import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional

import click

try:
    _version = version("web-scraper")
except PackageNotFoundError:
    _version = "dev"

from web_scraper.async_scrapers import WebScraperAsync
from web_scraper.config import config
from web_scraper.scrapers import WebScraper


@click.group()
@click.version_option(version=_version, prog_name="web-scraper")
def main() -> None:
    """Web Scraper - Extract content from any website URL."""
    pass


@main.command()
@click.argument("url", type=str)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Save output to a file (JSON format)",
)
@click.option(
    "--timeout",
    "-t",
    type=float,
    default=config.timeout,
    help=f"Request timeout in seconds (default: {config.timeout})",
)
@click.option(
    "--max-links",
    type=int,
    default=config.max_links,
    help=f"Maximum links to extract (default: {config.max_links})",
)
@click.option(
    "--pretty",
    "-p",
    is_flag=True,
    default=False,
    help="Pretty print JSON output",
)
@click.option(
    "--playwright",
    is_flag=True,
    default=False,
    help="Use Playwright for JavaScript-rendered pages (Cloudflare, reCAPTCHA)",
)
@click.option(
    "--screenshot",
    type=click.Path(),
    default=None,
    help="Save screenshot when using Playwright",
)
def scrape(
    url: str,
    output: str,
    timeout: float,
    max_links: int,
    pretty: bool,
    playwright: bool,
    screenshot: Optional[str],
) -> None:
    """Scrape content from a URL and output as JSON."""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    if playwright:
        import asyncio

        try:
            from web_scraper.playwright_scrapers import PlaywrightScraper

            async def scrape_with_playwright():
                async with PlaywrightScraper(timeout=int(timeout * 1000)) as scraper:
                    if screenshot:
                        return await scraper.scrape_with_screenshot(url, screenshot)
                    return await scraper.scrape(url)

            result = asyncio.run(scrape_with_playwright())
        except ImportError as e:
            click.echo(f"Error: {str(e)}")
            return
    else:
        with WebScraper(timeout=timeout, max_links=max_links) as scraper:
            result = scraper.scrape(url)

    output_data = result.to_dict()

    if output:
        Path(output).write_text(json.dumps(output_data, indent=2 if pretty else None))
        click.echo(f"✓ Saved to {output}")
    else:
        click.echo(json.dumps(output_data, indent=2 if pretty else None))


@main.command()
@click.option(
    "--host",
    type=str,
    default=config.api_host,
    help=f"API host (default: {config.api_host})",
)
@click.option(
    "--port",
    type=int,
    default=config.api_port,
    help=f"API port (default: {config.api_port})",
)
@click.option(
    "--reload",
    is_flag=True,
    default=False,
    help="Enable auto-reload for development",
)
def serve(host: str, port: int, reload: bool) -> None:
    """Start the REST API server."""
    import uvicorn

    from web_scraper.api import app

    click.echo(f"Starting API server at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, reload=reload)


@main.command()
@click.argument("urls", nargs=-1, required=True)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    required=True,
    help="Output file for batch results (JSON lines format)",
)
@click.option(
    "--timeout",
    "-t",
    type=float,
    default=config.timeout,
    help=f"Request timeout in seconds (default: {config.timeout})",
)
@click.option(
    "--concurrent",
    "-c",
    type=int,
    default=5,
    help="Number of concurrent requests (default: 5)",
)
def batch(urls: list, output: str, timeout: float, concurrent: int) -> None:
    """Scrape multiple URLs in batch mode."""
    import asyncio

    async def scrape_all() -> list:
        async with WebScraperAsync(timeout=timeout) as scraper:
            return await scraper.scrape_batch(urls, concurrent)

    results = asyncio.run(scrape_all())

    with open(output, "w", encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(result.to_dict()) + "\n")

    click.echo(f"✓ Scraped {len(results)} URLs, saved to {output}")


@main.command()
@click.argument("command", type=str)
@click.option(
    "--model",
    type=str,
    default=config.default_research_model,
    help=f"Ollama model to use (default: {config.default_research_model})",
)
@click.option(
    "--host",
    type=str,
    default=config.ollama_host,
    help=f"Ollama API host (default: {config.ollama_host})",
)
def ai(command: str, model: str, host: str) -> None:
    """
    Use natural language to scrape websites.

    Examples:
        web-scraper ai "scrape anthropic site and get me the last 10 news"
        web-scraper ai "get the latest blog posts from openai.com"
        web-scraper ai "extract all products from example.com/store"
    """
    import asyncio

    from web_scraper.ai_agent import OllamaAgent

    # Initialize AI agent
    agent = OllamaAgent(model=model, host=host)

    # Check if Ollama is available (sync — called before event loop)
    if not agent.is_available():
        click.echo("Ollama is not running. Please start Ollama first:")
        click.echo("   ollama serve")
        click.echo(f"   ollama pull {config.default_research_model}")
        return

    click.echo(f'Processing: "{command}"')
    click.echo("Analyzing command...")

    async def _run_ai_command() -> None:
        # Process the natural language command (async)
        params = await agent.process_command(command)

        if not params["url"]:
            click.echo("Could not determine URL from command. Please provide a valid website.")
            return

        url = params["url"]
        mode = params["mode"]
        max_items = params["max_items"]
        filters = params["filters"]

        click.echo(f"URL: {url}")
        click.echo(f"Mode: {mode}")
        if max_items:
            click.echo(f"Max items: {max_items}")
        if filters:
            click.echo(f"Filters: {filters}")
        click.echo()

        # Scrape the website
        click.echo("Scraping website...")

        if mode == "playwright":
            from web_scraper.playwright_scrapers import PlaywrightScraper

            async with PlaywrightScraper() as scraper:
                result = await scraper.scrape(url)
        else:
            result = await asyncio.to_thread(lambda: WebScraper().__enter__().scrape(url))

        if result.error:
            click.echo(f"Error: {result.error}")
            return

        click.echo(f"Successfully scraped: {result.title}")
        click.echo()

        # Generate AI summary (async)
        click.echo("🧠 Generating summary...")
        click.echo()

        summary = await agent.summarize_content(
            result.content, filters=filters, max_items=max_items
        )

        # Output formatted results
        click.echo("=" * 60)
        click.echo(f"📰 {result.title}")
        click.echo(f"🔗 {result.url}")
        click.echo("=" * 60)
        click.echo()
        click.echo(summary)
        click.echo()
        click.echo("=" * 60)

        # Show metadata if available
        if result.metadata.get("description"):
            click.echo(f"Description: {result.metadata['description']}")

        # Show links summary
        internal_count = len(result.links.get("internal", []))
        external_count = len(result.links.get("external", []))
        click.echo(f"🔗 Links found: {internal_count} internal, {external_count} external")

    asyncio.run(_run_ai_command())


@main.command()
@click.argument("query", type=str)
@click.option(
    "--max-sources",
    type=int,
    default=None,
    help="Maximum number of sources to check (AI decides if not specified)",
)
@click.option(
    "--model",
    type=str,
    default=config.default_research_model,
    help=f"Ollama model to use (default: {config.default_research_model})",
)
@click.option(
    "--host",
    type=str,
    default=config.ollama_host,
    help=f"Ollama API host (default: {config.ollama_host})",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Save report to file",
)
@click.option(
    "--deep",
    is_flag=True,
    default=False,
    help="Deep research mode - get full content from each source",
)
@click.option(
    "--no-synthesis",
    is_flag=True,
    default=False,
    help="Skip AI synthesis, show detailed raw content from all sources",
)
def research(
    query: str,
    max_sources: Optional[int],
    model: str,
    host: str,
    output: Optional[str],
    deep: bool,
    no_synthesis: bool,
) -> None:
    """
    Perform comprehensive multi-source research on any topic.

    AI decides which sources to check (3-25) and synthesizes findings.
    Without --max-sources, AI automatically determines the optimal number.

    Examples:
        web-scraper research "latest developments in quantum computing"
        web-scraper research "climate change effects 2024"
        web-scraper research "best practices for python async" --deep
        web-scraper research "complex topic" --max-sources 15 --deep
    """
    import asyncio

    from web_scraper.research_agent import ResearchAgent

    # Initialize research agent
    agent = ResearchAgent(
        model=model,
        host=host,
        max_concurrent=config.research_max_concurrent_sources,
        timeout_per_source=config.research_timeout_per_source,
    )

    # Check if Ollama is available
    if not agent.is_available():
        click.echo("Ollama is not running. Please start Ollama first:")
        click.echo("   ollama serve")
        click.echo(f"   ollama pull {config.default_research_model}")
        return

    # Run research
    try:
        report = asyncio.run(
            agent.research(
                query,
                max_sources,
                deep_mode=deep,
                no_synthesis=no_synthesis,
                progress_sink=click.echo,
            )
        )

        # Format and display report
        formatted_report = agent.format_report(report, no_synthesis=no_synthesis)
        click.echo(formatted_report)

        # Save to file if requested
        if output:
            import json

            report_dict = {
                "query": report.query,
                "summary": report.summary,
                "key_findings": report.key_findings,
                "sources": [
                    {
                        "source": s.source,
                        "url": s.url,
                        "title": s.title,
                        "relevance_score": s.relevance_score,
                        "error": s.error,
                    }
                    for s in report.sources
                ],
                "sources_checked": report.sources_checked,
                "sources_succeeded": report.sources_succeeded,
            }
            with open(output, "w", encoding="utf-8") as f:
                json.dump(report_dict, f, indent=2)
            click.echo(f"\n💾 Report saved to: {output}")

    except Exception as e:
        click.echo(f"Research failed: {str(e)}")


if __name__ == "__main__":
    main()
