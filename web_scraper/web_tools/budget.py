"""Keep a tool's JSON answer under a character budget."""

from __future__ import annotations

import json
from typing import Any, Optional

ELLIPSIS = "…"
# A text cut to fit keeps at least this much, and evidence at least this much
# until nothing else is left to cut.
_MIN_TEXT = 200
_MIN_EVIDENCE = 80


def json_size(value: Any) -> int:
    """Characters of *value* as compact JSON, the form MCP and REST send."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def fit_extract_payload(payload: dict[str, Any], max_chars: int) -> bool:
    """Cut an extraction's values, then its evidence, until it fits *max_chars*.

    The largest field shrinks first: a list loses items from its end, a text
    its tail (ending in an ellipsis), and the field's provenance says so with
    ``truncated`` (and ``returned`` for a list). Field names and provenance
    stay. Returns whether anything was cut.
    """
    data: dict[str, Any] = payload.get("data") or {}
    provenance: dict[str, Any] = payload.get("provenance") or {}
    cut = False
    for _ in range(4 * len(data) + 8):
        excess = json_size(payload) - max_chars
        if excess <= 0:
            return cut
        name = _largest_shrinkable(data)
        if name is None:
            break
        data[name] = _shrink(data[name], excess)
        entry = provenance.get(name)
        if isinstance(entry, dict):
            entry["truncated"] = True
            if isinstance(data[name], list) and "returned" in entry:
                entry["returned"] = len(data[name])
        cut = True

    for floor in (_MIN_EVIDENCE, 0):
        for entry in sorted(
            (item for item in provenance.values() if isinstance(item, dict)),
            key=lambda item: -len(str(item.get("evidence") or "")),
        ):
            excess = json_size(payload) - max_chars
            if excess <= 0:
                return cut
            evidence = str(entry.get("evidence") or "")
            if len(evidence) > floor:
                entry["evidence"] = _cut_text(evidence, excess, floor)
                cut = True
    return cut


_MIN_PAGE_MARKDOWN = 200


def fit_scrape_batch(answer: dict[str, Any], max_chars: int) -> bool:
    """Cut a batch answer's pages until the whole fits *max_chars*; whether anything was cut.

    The Markdown goes first, and the pages that are over one shared length are cut to it
    (a long page loses most, a short one nothing), each ending in an ellipsis and marked
    ``markdown_truncated``. Only when that is not enough do the links, then the metadata,
    then the rest of the Markdown give way. A page's entry is replaced by key, never
    changed inside: the cached answers it came from stay whole.
    """
    pages = [page for page in answer["pages"] if "markdown" in page]
    cut = False

    def over() -> int:
        return json_size(answer) - max_chars

    for floor in (_MIN_PAGE_MARKDOWN, 0):
        for _ in range(6):
            excess = over()
            if excess <= 0:
                return cut
            level = _shared_level(
                [len(page["markdown"]) for page in pages],
                excess + len(pages) * len(ELLIPSIS),
                floor,
            )
            if level is None:
                break
            for page in pages:
                if len(page["markdown"]) > level:
                    page["markdown"] = page["markdown"][:level].rstrip() + ELLIPSIS
                    page["markdown_truncated"] = True
                    cut = True
        if floor == _MIN_PAGE_MARKDOWN and over() > 0:
            for key, flag in (("links", "links_truncated"), ("metadata", "metadata_truncated")):
                for page in pages:
                    if page.get(key) and over() > 0:
                        page[key] = [] if key == "links" else {}
                        page[flag] = True
                        cut = True
    return cut


def _shared_level(lengths: list[int], need: int, floor: int) -> Optional[int]:
    """The highest length that, taken as the cap of every longer text, removes *need* characters.

    None when even the floor does not remove that many.
    """
    if sum(max(0, length - floor) for length in lengths) < need:
        return None
    low, high = floor, max(lengths, default=floor)
    while low < high:
        middle = (low + high + 1) // 2
        if sum(max(0, length - middle) for length in lengths) >= need:
            low = middle
        else:
            high = middle - 1
    return low


# A scrape's metadata: the page's meta tags and JSON-LD, which a page can make
# as large as it likes (an articleBody repeating the text, a catalogue of
# products), bounded as the Markdown is.
METADATA_MAX_CHARS = 10_000
_METADATA_TEXT_CHARS = 500
_METADATA_LIST_ITEMS = 20


def trim_metadata(
    metadata: dict[str, Any], max_chars: int = METADATA_MAX_CHARS
) -> tuple[dict[str, Any], bool]:
    """A page's metadata within *max_chars*, and whether anything was cut.

    Every text keeps at most 500 characters and every list 20 items; meta tags
    are kept while they fill half the budget, then JSON-LD items while the
    whole fits. What is kept keeps its shape.
    """
    cut = False

    def shorten(value: Any) -> Any:
        nonlocal cut
        if isinstance(value, str) and len(value) > _METADATA_TEXT_CHARS:
            cut = True
            return value[:_METADATA_TEXT_CHARS].rstrip() + ELLIPSIS
        if isinstance(value, list):
            if len(value) > _METADATA_LIST_ITEMS:
                cut = True
            return [shorten(item) for item in value[:_METADATA_LIST_ITEMS]]
        if isinstance(value, dict):
            return {key: shorten(item) for key, item in value.items()}
        return value

    trimmed: dict[str, Any] = {}
    for key, value in metadata.items():
        if key == "json_ld":
            continue
        kept = shorten(value)
        if json_size(trimmed) + json_size({key: kept}) > max_chars // 2:
            cut = True
            continue
        trimmed[key] = kept
    items: list[Any] = []
    for item in metadata.get("json_ld") or []:
        kept = shorten(item)
        if json_size(trimmed) + json_size(items) + json_size(kept) > max_chars:
            cut = True
            break
        items.append(kept)
    if "json_ld" in metadata:
        trimmed["json_ld"] = items
    return trimmed, cut


def _shrinkable(value: Any) -> bool:
    if isinstance(value, list):
        return len(value) > 1 or (len(value) == 1 and _shrinkable(value[0]))
    return isinstance(value, str) and len(value) > _MIN_TEXT


def _largest_shrinkable(data: dict[str, Any]) -> Optional[str]:
    sizes = [(json_size(value), name) for name, value in data.items() if _shrinkable(value)]
    return max(sizes)[1] if sizes else None


def _shrink(value: Any, excess: int) -> Any:
    if isinstance(value, list):
        if len(value) == 1:
            return [_shrink(value[0], excess)]
        keep, shed = len(value), 0
        while keep > 1 and shed < excess:
            keep -= 1
            shed += json_size(value[keep]) + 1
        return value[:keep]
    return _cut_text(value, excess, _MIN_TEXT)


def _cut_text(text: str, excess: int, floor: int) -> str:
    if floor == 0 and excess >= len(text):
        return ""
    keep = max(floor, len(text) - excess - len(ELLIPSIS))
    if keep >= len(text):
        return text
    return text[:keep].rstrip() + ELLIPSIS
