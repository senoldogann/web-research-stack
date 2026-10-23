"""A scrape's metadata stays small: JSON-LD and meta tags are bounded like the Markdown."""

from __future__ import annotations

import json

import pytest
from test_web_service import ARTICLE_HTML, FakeFetcher, _html_result, _service

URL = "https://news.example.com/2026/10/23/big-story"


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _page(json_ld: list[dict], meta: str = "") -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(item)}</script>' for item in json_ld
    )
    return ARTICLE_HTML.replace("</head>", f"{meta}{scripts}</head>")


@pytest.mark.asyncio
async def test_a_page_with_huge_json_ld_answers_in_a_bounded_size() -> None:
    article = {
        "@type": "NewsArticle",
        "headline": "Big story",
        "articleBody": "A sentence of the story. " * 4000,
        "about": [{"@type": "Thing", "name": f"Topic {index}"} for index in range(500)],
    }
    products = [
        {"@type": "Product", "name": f"Item {index}", "description": "x" * 400}
        for index in range(19)
    ]
    huge_meta = '<meta name="huge" content="' + ("y" * 5000) + '">'
    service = _service(
        FakeFetcher({URL: _html_result(URL, _page([article, *products], huge_meta))})
    )

    result = await service.scrape(URL)

    assert _size(result["metadata"]) <= 11_000
    assert result["metadata_truncated"] is True
    kept = result["metadata"]["json_ld"][0]
    assert kept["headline"] == "Big story"
    assert kept["articleBody"].endswith("…") and len(kept["articleBody"]) <= 501
    assert len(kept["about"]) == 20
    assert len(result["metadata"]["huge"]) <= 501
    assert result["metadata"]["description"] == "Article description"


@pytest.mark.asyncio
async def test_a_page_with_small_metadata_is_untouched() -> None:
    item = {"@type": "NewsArticle", "headline": "Small story", "datePublished": "2026-10-23"}
    service = _service(FakeFetcher({URL: _html_result(URL, _page([item]))}))

    result = await service.scrape(URL)

    assert result["metadata_truncated"] is False
    assert result["metadata"]["json_ld"] == [item]
