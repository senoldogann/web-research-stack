# Production Deployment Runbook

This runbook covers the production-oriented stack included in this repository.

## Included Artifacts

- [Dockerfile.backend](../Dockerfile.backend)
- [Dockerfile](../web-ui/Dockerfile)
- [docker-compose.production.yml](../docker-compose.production.yml)
- [nginx.conf](../ops/nginx/nginx.conf)
- [prometheus.yml.tmpl](../ops/prometheus/prometheus.yml.tmpl)
- [alerts.yml](../ops/prometheus/alerts.yml)
- [web-research-overview.json](../ops/grafana/dashboards/web-research-overview.json)
- [rotate_local_secrets.py](../scripts/rotate_local_secrets.py)
- [smoke_test_deployment.py](../scripts/smoke_test_deployment.py)
- [verify_all.py](../scripts/verify_all.py)

## Feature Overview

### Research Profiles
Deep-mode searches can be steered with a `research_profile` field (`technical` / `news` / `academic`). Each profile adds dedicated OSS data collectors (Wikipedia + StackExchange, HackerNews + RSS feeds, arXiv + PubMed) on top of standard DuckDuckGo/Google queries.

### Citation Faithfulness
Every synthesized response includes a `citation_audit` block with a `faithfulness_score` (0–1) based on Jaccard keyword overlap between citation markers and the referenced source content.

### Network Resilience
- DuckDuckGo searches: 3 retries with 1.5 s base delay + jitter
- Google searches: 2 retries with 2.0 s base delay + jitter
- Playwright fallback: if HTTP scraper returns < 250 chars, the system escalates to a headless Chromium browser automatically

### Playwright in Docker
`Dockerfile.backend` installs `playwright` and `playwright-stealth` via `pip install ".[playwright]"` and downloads the Chromium headless-shell binary via `playwright install --with-deps chromium`. The binary is stored in `/app/.playwright` and is accessible by the non-root `appuser`. This increases the image size by ~200–300 MB compared to a non-Playwright build.

### Observability
New Prometheus histogram metrics are emitted per request:
- `web_scraper_request_duration_seconds` — latency histogram by endpoint/method
- `web_scraper_upstream_failures_total` — 5xx upstream error counter
- `web_scraper_circuit_breaker_state` — gauge (1=open, 0=closed) per circuit breaker

## Secret Handling

Local ignored env files now carry strong generated secrets.

- Backend secrets live in [.env](../.env.example) (copy `.env.example` → `.env`)
- Frontend proxy secret lives in [web-ui/.env.local](../web-ui/.env.local.example) (copy `.env.local.example` → `.env.local`)
- Prometheus authentication uses `PROMETHEUS_API_KEY` (if set) or the first key from `API_KEYS`.

Rotate them with:

```bash
python3 scripts/rotate_local_secrets.py
```

## Start The Production-Like Stack

```bash
docker compose -f docker-compose.production.yml up --build
```

Services:

- App entrypoint: `http://localhost`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3001` (default credentials: `admin` / `admin`)

> **Note:** Change the default Grafana password on first login in production.

## Smoke Test

```bash
python3 scripts/smoke_test_deployment.py \
  --base-url http://127.0.0.1 \
  --api-key "$(python3 - <<'PY'
from pathlib import Path
for line in Path('.env').read_text().splitlines():
    if line.startswith('API_KEYS='):
        print(line.split('=', 1)[1])
        break
PY
)"
```

## Release Checklist

Before deploying a new version, run the automated release gate:

```bash
python3 scripts/verify_all.py
```

This script checks:
- Required module files exist (`citation_verifier.py`, `retry_utils.py`, `profile_collectors.py`, etc.)
- Core modules import cleanly under Python
- `ruff` lint passes with no errors
- All tests pass and the passing count meets the minimum threshold (≥ 30)
- Next.js frontend builds successfully

Deployment should only proceed when the script exits with **"ALL SYSTEMS GREEN"**.

1. Replace local hostnames in `API_ALLOWED_ORIGINS` and `API_TRUSTED_HOSTS` with real production domains.
2. Point `OLLAMA_HOST` to the actual production model runtime.
3. Put TLS termination in front of Nginx or add HTTPS at the edge (e.g., Cloudflare, AWS ALB, or Caddy as a reverse proxy with automatic Let's Encrypt certificates).
4. Keep `SCRAPER_ALLOW_PRIVATE_NETWORKS=false`.
5. Change default Grafana admin password.
6. Run the smoke test against the public domain before opening traffic.
7. Review Prometheus alerts and Grafana dashboard after the first live traffic window.
