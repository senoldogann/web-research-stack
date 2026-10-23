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
