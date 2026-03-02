"""Rotate ignored local runtime secrets for the backend and frontend."""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

ROOT_ENV = Path(".env")
FRONTEND_ENV = Path("web-ui/.env.local")


def _generate_secret(bytes_length: int) -> str:
    """Generate a hex-encoded secret."""
    return secrets.token_hex(bytes_length)


def _replace_or_append(path: Path, replacements: dict[str, str]) -> None:
    """Update dotenv-style key/value files without disturbing comments."""
    lines = path.read_text().splitlines()
    seen = set()
    updated_lines = []

    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            updated_lines.append(line)
            continue

        key, _, _ = line.partition("=")
        key = key.strip()
        if key not in replacements:
            updated_lines.append(line)
            continue

        updated_lines.append(f"{key}={replacements[key]}")
        seen.add(key)

    for key, value in replacements.items():
        if key not in seen:
            updated_lines.append(f"{key}={value}")

    path.write_text("\n".join(updated_lines) + "\n")


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-key-bytes",
        type=int,
        default=32,
        help="Random bytes used for the backend API key",
    )
    parser.add_argument(
        "--grafana-password-bytes",
        type=int,
        default=24,
        help="Random bytes used for the Grafana admin password",
    )
    parser.add_argument(
        "--alert-token-bytes",
        type=int,
        default=24,
        help="Random bytes used for the Prometheus alert webhook token",
    )
    args = parser.parse_args()

    api_key = _generate_secret(args.api_key_bytes)
    grafana_password = _generate_secret(args.grafana_password_bytes)
    alert_token = _generate_secret(args.alert_token_bytes)

    _replace_or_append(
        ROOT_ENV,
        {
            "API_KEYS": api_key,
            "GRAFANA_ADMIN_PASSWORD": grafana_password,
            "PROMETHEUS_ALERT_WEBHOOK_TOKEN": alert_token,
        },
    )
    _replace_or_append(
        FRONTEND_ENV,
        {
            "BACKEND_API_KEY": api_key,
        },
    )

    print("Rotated local secrets in .env and web-ui/.env.local")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
