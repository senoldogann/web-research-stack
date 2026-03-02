"""Run a production-style smoke test against the deployed API."""

from __future__ import annotations

import argparse
import sys

import httpx


def _assert_ok(response: httpx.Response, label: str) -> None:
    """Raise a descriptive error when a smoke check fails."""
    if response.is_success:
        return
    raise RuntimeError(f"{label} failed with status {response.status_code}: {response.text[:500]}")


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1", help="Public base URL")
    parser.add_argument("--api-key", required=True, help="API key for protected endpoints")
    parser.add_argument(
        "--query",
        default="latest ai coding agents",
        help="Research query used for the end-to-end check",
    )
    parser.add_argument(
        "--skip-research",
        action="store_true",
        help="Only validate health and tool discovery",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    headers = {"X-API-Key": args.api_key}

    with httpx.Client(timeout=120.0) as client:
        health = client.get(f"{base_url}/api/v1/health")
        _assert_ok(health, "health")

        tools = client.get(f"{base_url}/api/v1/tools", headers=headers)
        _assert_ok(tools, "tools manifest")

        if not args.skip_research:
            research = client.post(
                f"{base_url}/api/v1/tools/web-research",
                headers=headers,
                json={
                    "query": args.query,
                    "max_sources": 3,
                    "deep_mode": False,
                },
            )
            _assert_ok(research, "web research")
            payload = research.json()
            if not payload.get("answer"):
                raise RuntimeError("web research returned an empty answer")

    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
