"""Tests for PDF scraping: detection, text extraction, and clear failure codes."""

from __future__ import annotations

import io
from typing import Optional

import httpx
import pytest
from pypdf import PdfReader, PdfWriter

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult, SafeFetcher
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.pdf import PdfError, extract_pdf, looks_like_pdf
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

PDF_URL = "https://arxiv.example/pdf/1706.03762"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[str], *, title: Optional[str] = None) -> bytes:
    """Build a small, valid PDF whose pages each show one line of Helvetica text."""
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_ids: list[int] = []
    next_id = 4
    for text in pages:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        page_ids.append(page_id)
        stream = f"BT /F1 18 Tf 72 720 Td ({_escape(text)}) Tj ET".encode("latin-1")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    info_ref = ""
    if title is not None:
        objects[next_id] = f"<< /Title ({_escape(title)}) /Author (A. Author) >>".encode()
        info_ref = f" /Info {next_id} 0 R"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for object_id in sorted(objects):
        offsets[object_id] = len(out)
        out += f"{object_id} 0 obj\n".encode() + objects[object_id] + b"\nendobj\n"
    xref_at = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for object_id in range(1, size):
        out += f"{offsets[object_id]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R{info_ref} >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def encrypt_pdf(data: bytes, *, user_password: str, owner_password: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(
        user_password=user_password,
        owner_password=owner_password,
        algorithm="RC4-128",
    )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


ATTENTION = make_pdf(
    ["Attention Is All You Need", "The Transformer uses multi-head attention."],
    title="Attention Is All You Need",
)


class TestExtractPdf:
    def test_reads_every_page_into_markdown_with_page_sections(self) -> None:
        document = extract_pdf(ATTENTION, max_chars=10_000)

        assert document.pages == 2
        assert document.pages_read == 2
        assert document.title == "Attention Is All You Need"
        assert document.markdown.startswith("# Attention Is All You Need")
        assert "## Page 1\n\nAttention Is All You Need" in document.markdown
        assert "## Page 2\n\nThe Transformer uses multi-head attention." in document.markdown
        assert document.metadata["author"] == "A. Author"
        assert document.warnings == []

    def test_stops_reading_at_the_character_budget(self) -> None:
        data = make_pdf([f"Page number {index} text" for index in range(1, 7)])

        document = extract_pdf(data, max_chars=40)

        assert document.pages == 6
        assert document.pages_read < 6
        assert len(document.markdown) <= 40
        assert "pdf_truncated" in document.warnings

    def test_ignores_a_file_name_as_title(self) -> None:
        document = extract_pdf(make_pdf(["Body"], title="report-final.docx"), max_chars=1_000)

        assert document.title == ""
        assert document.markdown.startswith("## Page 1")

    def test_password_protected_pdf_is_pdf_encrypted(self) -> None:
        locked = encrypt_pdf(ATTENTION, user_password="secret", owner_password="owner")

        with pytest.raises(PdfError) as exc:
            extract_pdf(locked, max_chars=1_000)

        assert exc.value.code == "pdf_encrypted"

    def test_owner_password_only_pdf_still_opens(self) -> None:
        restricted = encrypt_pdf(ATTENTION, user_password="", owner_password="owner")

        document = extract_pdf(restricted, max_chars=10_000)

        assert "Attention Is All You Need" in document.markdown
        assert document.metadata["encrypted"] is True

    @pytest.mark.parametrize(
        "data",
        [b"%PDF-1.7\nthis is not a pdf body", b"", ATTENTION[: len(ATTENTION) // 3]],
    )
    def test_broken_pdf_is_pdf_invalid(self, data: bytes) -> None:
        with pytest.raises(PdfError) as exc:
            extract_pdf(data, max_chars=1_000)

        assert exc.value.code == "pdf_invalid"

    def test_pdf_without_text_warns(self) -> None:
        document = extract_pdf(make_pdf([""]), max_chars=1_000)

        assert document.markdown == ""
        assert "pdf_no_text" in document.warnings


def test_pdf_is_recognised_by_type_or_magic_bytes() -> None:
    assert looks_like_pdf("application/pdf", b"")
    assert looks_like_pdf("application/x-pdf", b"")
    assert looks_like_pdf("application/octet-stream", b"%PDF-1.5\n...")
    assert not looks_like_pdf("application/octet-stream", b"00")
    assert not looks_like_pdf("text/html", b"<html>%PDF-1.5</html>")


def _allow(url: str, allow_private_networks: bool = False) -> str:
    return url


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["application/pdf", "application/octet-stream"])
async def test_fetcher_returns_pdf_bytes(content_type: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type}, content=ATTENTION)

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
    ) as fetcher:
        result = await fetcher.fetch(PDF_URL)

    assert result.content_type == "application/pdf"
    assert result.content == ATTENTION
    assert result.html == ""


@pytest.mark.asyncio
async def test_fetcher_keeps_pdfs_inside_the_byte_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=ATTENTION)

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
        max_response_bytes=100,
    ) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch(PDF_URL)

    assert exc.value.code == "too_large"


class PdfFetcher:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            content_type="application/pdf",
            html="",
            network_ms=5.0,
            total_ms=6.0,
            content=self.data,
        )


def _service(data: bytes) -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(),
        fetcher=PdfFetcher(data),  # type: ignore[arg-type]
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


@pytest.mark.asyncio
async def test_scrape_returns_pdf_text_with_content_type_and_pages() -> None:
    result = await _service(ATTENTION).scrape(PDF_URL)

    assert result["content_type"] == "application/pdf"
    assert result["pages"] == 2
    assert "Attention Is All You Need" in result["markdown"]
    assert result["title"] == "Attention Is All You Need"
    assert result["fetched_with"] == "http"
    assert result["links"] == []
    assert result["metadata"]["pdf"]["author"] == "A. Author"
    assert result["timings"]["extract_ms"] >= 0
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_html_scrape_has_no_pages_field() -> None:
    url = "https://example.com/a"

    class HtmlFetcher:
        async def fetch(self, target: str) -> FetchResult:
            return FetchResult(target, target, 200, "text/html", "<main><p>Hello</p></main>")

    service = WebToolsService(
        settings=WebToolsSettings(),
        fetcher=HtmlFetcher(),  # type: ignore[arg-type]
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )

    result = await service.scrape(url)

    assert "pages" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "code"),
    [
        (encrypt_pdf(ATTENTION, user_password="secret", owner_password="owner"), "pdf_encrypted"),
        (b"%PDF-1.4\ngarbage", "pdf_invalid"),
    ],
)
async def test_scrape_reports_unreadable_pdfs_with_clear_codes(data: bytes, code: str) -> None:
    with pytest.raises(WebToolError) as exc:
        await _service(data).scrape(PDF_URL)

    assert exc.value.code == code
    assert exc.value.http_status == 422
