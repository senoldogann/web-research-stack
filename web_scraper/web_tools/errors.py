"""Stable errors returned by the local web tools."""

from __future__ import annotations

from typing import Any, Optional

_STATUS_CODES = {
    "invalid_request": 422,
    "unsafe_target": 400,
    "dns_error": 502,
    "blocked": 422,
    "timeout": 504,
    "too_large": 413,
    "unsupported_content_type": 415,
    "pdf_encrypted": 422,
    "pdf_invalid": 422,
    "http_error": 502,
    "network_error": 502,
    "browser_unavailable": 503,
    "empty_content": 502,
    "search_failed": 502,
    "job_not_found": 404,
    "crawl_capacity": 429,
    "internal_error": 500,
}


class WebToolError(Exception):
    """A classified, user-safe web tool failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        url: Optional[str] = None,
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
        hint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.url = url
        self.status_code = status_code
        self.detail = detail
        # What a caller can try next; only in the error when there is something to try.
        self.hint = hint

    @property
    def http_status(self) -> int:
        return _STATUS_CODES.get(self.code, 400)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "url": self.url,
            "status_code": self.status_code,
            "detail": self.detail,
        }
        if self.hint:
            payload["hint"] = self.hint
        return payload
