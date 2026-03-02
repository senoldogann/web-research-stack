"""Sanitization helpers for scraped content before LLM synthesis."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bs4 import BeautifulSoup, Tag

CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
WHITESPACE_RE = re.compile(r"[ \t]{2,}")
# Dangerous URL schemes that could inject scripts or execute arbitrary code.
# Covers javascript:, vbscript:, and data: URIs that embed HTML/JS payloads.
URL_SCRIPT_RE = re.compile(
    r"(?:javascript|vbscript|data:(?:text/html|application/[a-z+\-]+)):[^\s]*",
    re.IGNORECASE,
)

# Regex to match markdown fenced code blocks (``` ... ```)
_CODE_FENCE_RE = re.compile(r"(```[\w]*\n.*?\n```)", re.DOTALL)

# ------------------------------------------------------------------
# Code-block preservation helpers
# ------------------------------------------------------------------

_LANG_CLASS_PREFIXES = ("language-", "lang-", "highlight-source-", "brush:")


def _detect_language_from_classes(classes: list[str]) -> str:
    """Extract programming language from CSS class list on a <code> or <pre> tag."""
    for cls in classes:
        for prefix in _LANG_CLASS_PREFIXES:
            if cls.startswith(prefix):
                return cls[len(prefix) :]
        # Some sites use bare language names as classes
        if cls in {
            "python",
            "javascript",
            "typescript",
            "java",
            "c",
            "cpp",
            "csharp",
            "go",
            "rust",
            "ruby",
            "php",
            "swift",
            "kotlin",
            "scala",
            "sql",
            "bash",
            "shell",
            "html",
            "css",
            "json",
            "yaml",
            "xml",
            "r",
            "perl",
            "lua",
            "dart",
            "haskell",
            "elixir",
            "clojure",
            "jsx",
            "tsx",
        }:
            return cls
    return ""


def preserve_code_blocks(soup: "BeautifulSoup") -> None:
    """Convert <pre>/<code> HTML elements to markdown fenced code blocks in-place.

    Must be called BEFORE ``get_text()`` so that code structure survives
    the HTML→plain-text conversion.  Works by replacing each ``<pre>`` (or
    standalone multi-line ``<code>``) with a NavigableString containing a
    properly fenced markdown code block.
    """
    from bs4 import NavigableString

    # --- Handle <pre> blocks (the primary code container) ---
    for pre_tag in soup.find_all("pre"):
        code_tag = pre_tag.find("code")
        if code_tag:
            classes = code_tag.get("class", []) or []
            lang = _detect_language_from_classes(classes)
            # get_text() without strip to preserve internal whitespace
            code_text = code_tag.get_text()
        else:
            classes = pre_tag.get("class", []) or []
            lang = _detect_language_from_classes(classes)
            code_text = pre_tag.get_text()

        # Clean up but keep indentation: only strip trailing whitespace per line
        lines = code_text.split("\n")
        # Remove leading/trailing empty lines
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        code_text = "\n".join(lines)

        if not code_text.strip():
            pre_tag.decompose()
            continue

        fence = f"\n\n```{lang}\n{code_text}\n```\n\n"
        pre_tag.replace_with(NavigableString(fence))

    # --- Handle standalone <code> not inside <pre> (inline code) ---
    for code_tag in soup.find_all("code"):
        # Skip if parent is <pre> (already handled above — shouldn't exist
        # after the loop above, but guard against edge cases)
        if code_tag.parent and code_tag.parent.name == "pre":
            continue
        code_text = code_tag.get_text()
        if not code_text.strip():
            continue
        # Multi-line standalone code → fenced block
        if "\n" in code_text and len(code_text) > 60:
            classes = code_tag.get("class", []) or []
            lang = _detect_language_from_classes(classes)
            fence = f"\n\n```{lang}\n{code_text}\n```\n\n"
            code_tag.replace_with(NavigableString(fence))
        else:
            # Inline code → backtick wrapping
            code_tag.replace_with(NavigableString(f"`{code_text}`"))


# ------------------------------------------------------------------
# Main sanitization
# ------------------------------------------------------------------


def sanitize_scraped_text(text: str, max_chars: int) -> str:
    """Normalize untrusted content into safer plain text for downstream models.

    Preserves markdown fenced code blocks (``` ... ```) so that indentation
    inside code examples is not destroyed by whitespace normalization.
    """
    cleaned = html.unescape(text)
    cleaned = URL_SCRIPT_RE.sub("", cleaned)
    cleaned = CONTROL_CHARS_RE.sub("", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")

    # --- Protect code blocks from whitespace normalization ---
    code_blocks: list[str] = []

    def _save_block(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x02CODEBLOCK{len(code_blocks) - 1}\x02"

    cleaned = _CODE_FENCE_RE.sub(_save_block, cleaned)

    # Normalize whitespace ONLY in non-code regions
    cleaned = WHITESPACE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        cleaned = cleaned.replace(f"\x02CODEBLOCK{i}\x02", block)

    cleaned = cleaned.strip()

    if len(cleaned) > max_chars:
        # Try not to cut in the middle of a code block
        truncated = cleaned[:max_chars]
        # If we're inside an unclosed code fence, find the last complete one
        open_fences = truncated.count("```")
        if open_fences % 2 != 0:
            # Find the start of the last incomplete code block
            last_fence = truncated.rfind("```")
            if last_fence > 0:
                truncated = truncated[:last_fence].rstrip()
        cleaned = truncated + "\n\n[Content truncated for safety.]"

    return cleaned


def summarize_snippet(text: str, max_chars: int = 280) -> str:
    """Create short source snippets for citations."""
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) > max_chars:
        return compact[:max_chars].rstrip() + "..."
    return compact
