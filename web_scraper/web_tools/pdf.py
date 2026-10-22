"""Text extraction for PDF documents fetched by the web tools."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from pypdf import PdfReader
from pypdf.errors import DependencyError, FileNotDecryptedError

PDF_CONTENT_TYPE = "application/pdf"
PDF_CONTENT_TYPES = frozenset({PDF_CONTENT_TYPE, "application/x-pdf"})
# Text stops at the caller's character budget; this bounds the work on a long
# document whose pages carry little text.
MAX_PDF_PAGES = 500
_FILENAME_TITLE = re.compile(r"\.(docx?|pdf|tex|dvi|ps|rtf|odt|indd|pptx?)$", re.IGNORECASE)


class PdfError(Exception):
    """A PDF that cannot be read: ``pdf_encrypted`` or ``pdf_invalid``."""

    def __init__(self, code: str, message: str, *, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class PdfDocument:
    title: str
    description: str
    markdown: str
    pages: int
    pages_read: int
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def looks_like_pdf(content_type: str, body: bytes) -> bool:
    """Whether a response is a PDF: by its declared type, or by its magic bytes."""
    return content_type in PDF_CONTENT_TYPES or body[:1024].lstrip().startswith(b"%PDF-")


def extract_pdf(data: bytes, *, max_chars: int) -> PdfDocument:
    """Read a PDF's text into Markdown, one ``## Page N`` section per page.

    Pages are read until ``max_chars`` of Markdown is collected; ``pages`` is
    still the document's full page count. A PDF that only has an owner
    password opens like any other; one that needs a user password raises
    ``pdf_encrypted``, and one that cannot be parsed raises ``pdf_invalid``.
    """
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        encrypted = reader.is_encrypted
        if encrypted and not reader.decrypt(""):
            raise PdfError("pdf_encrypted", "The PDF is protected by a password")
        total = len(reader.pages)
    except PdfError:
        raise
    except (DependencyError, FileNotDecryptedError, NotImplementedError) as exc:
        raise PdfError(
            "pdf_encrypted",
            "The PDF is encrypted in a way this server cannot open",
            detail=type(exc).__name__,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - untrusted bytes; any parser failure is a bad PDF
        raise PdfError(
            "pdf_invalid",
            "The PDF could not be parsed",
            detail=type(exc).__name__,
        ) from exc

    info = _document_info(reader)
    title = _meaningful_title(info.get("title"))
    sections: list[str] = [f"# {title}"] if title else []
    length = len(sections[0]) if sections else 0
    pages_read = 0
    pages_with_text = 0
    for index in range(min(total, MAX_PDF_PAGES)):
        if length >= max_chars:
            break
        pages_read += 1
        try:
            text = _clean_page_text(reader.pages[index].extract_text() or "")
        except (DependencyError, FileNotDecryptedError) as exc:
            raise PdfError(
                "pdf_encrypted",
                "The PDF is encrypted in a way this server cannot open",
                detail=type(exc).__name__,
            ) from exc
        except Exception:  # noqa: BLE001 - one unreadable page must not lose the others
            text = ""
        if not text:
            continue
        pages_with_text += 1
        section = f"## Page {index + 1}\n\n{text}"
        sections.append(section)
        length += len(section) + 2

    markdown = "\n\n".join(sections)
    warnings: list[str] = []
    if pages_with_text == 0:
        warnings.append("pdf_no_text")
    if pages_read < total or len(markdown) > max_chars:
        warnings.append("pdf_truncated")

    metadata = {key: value for key, value in info.items() if value and key != "title"}
    metadata["encrypted"] = encrypted
    return PdfDocument(
        title=title,
        description=info.get("subject") or "",
        markdown=markdown[:max_chars],
        pages=total,
        pages_read=pages_read,
        metadata=metadata,
        warnings=warnings,
    )


def _document_info(reader: PdfReader) -> dict[str, str]:
    try:
        raw = reader.metadata or {}
    except Exception:  # noqa: BLE001 - a broken info dictionary is not a broken document
        return {}
    keys = {
        "/Title": "title",
        "/Author": "author",
        "/Subject": "subject",
        "/Creator": "creator",
        "/Producer": "producer",
        "/CreationDate": "created",
        "/ModDate": "modified",
    }
    info: dict[str, str] = {}
    for key, name in keys.items():
        try:
            value = raw.get(key)
            if hasattr(value, "get_object"):
                value = value.get_object()
        except Exception:  # noqa: BLE001 - indirect objects may point nowhere
            continue
        if isinstance(value, (str, bytes)):
            text = value.decode("latin-1") if isinstance(value, bytes) else value
            text = " ".join(text.replace("\x00", "").split())
            if text:
                info[name] = text[:500]
    return info


def _meaningful_title(value: Optional[str]) -> str:
    title = (value or "").strip()
    if title.lower().startswith("microsoft word - "):
        title = title[len("microsoft word - ") :]
    if not title or title.lower() in {"untitled", "title"} or _FILENAME_TITLE.search(title):
        return ""
    return title[:300]


def _clean_page_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\x00", "").splitlines()]
    joined = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", joined)
