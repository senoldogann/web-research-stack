# Local Web Research Tools

A Firecrawl-style core for this repository: URL discovery, single-page scraping,
site mapping, background crawling, and structured extraction. The same service is
exposed over the REST API and over a stdio MCP server that Dot Desktop can run.

## Capabilities

| Capability | REST | MCP tool (Dot name) |
|---|---|---|
| Search for URLs | `POST /api/v1/web/search` | `web_search` (`mcp__web-research__web_search`) |
| Scrape one page | `POST /api/v1/web/scrape` | `web_scrape` (`mcp__web-research__web_scrape`) |
| Map a site | `POST /api/v1/web/map` | `web_map` (`mcp__web-research__web_map`) |
| Extract fields | `POST /api/v1/web/extract` | `web_extract` (`mcp__web-research__web_extract`) |
| Start a crawl | `POST /api/v1/web/crawl` | `web_crawl_start` |
| Crawl status | `GET /api/v1/web/crawl/{job_id}` | `web_crawl_status` |
| Crawl results | `GET /api/v1/web/crawl/{job_id}/results` | `web_crawl_results` |
| Cancel a crawl | `POST /api/v1/web/crawl/{job_id}/cancel` | `web_crawl_cancel` |
| Lists / metrics | `GET /api/v1/web/crawls`, `/api/v1/web/stats` | — |

All REST routes use the existing API-key auth (`X-API-Key` or Bearer) and the new
`API_WEB_RATE_LIMIT_PER_MINUTE` budget (default 60/min). The MCP server is a local
stdio process and needs no API key.

## Architecture

```
Dot Desktop ──stdio JSON-RPC──> web_scraper/mcp_server.py ─┐
REST clients ────────────────> web_scraper/api.py ────────┼─> WebToolsService
scripts/benchmark_web_tools.py ───────────────────────────┘
                                                             ├─ SafeFetcher (httpx)
                                                             ├─ HeadlessBrowserManager
                                                             ├─ search adapters
                                                             ├─ content extraction
                                                             ├─ TTL cache + latency
                                                             └─ CrawlManager
```

- `WebToolsService` is the single implementation behind REST, MCP, and the
  benchmark; there is no second scrape path to drift.
- `SafeFetcher` is HTTP-first. It validates every redirect hop, resolves DNS
  through `validate_outbound_url`, caps response bytes, retries transient
  failures with exponential backoff, and applies global and per-host gates.
- `HeadlessBrowserManager` owns one long-lived browser. `auto` tries headless
  Chrome, then Playwright Chromium, then headless Camoufox. Each render gets a
  fresh isolated context; the worker is reused across requests.
- Search is an adapter (`duckduckgo` default, `google` optional). It only
  discovers URLs; reading a result goes through `web_scrape`.
- Extraction is a pure content layer (`web_scraper/web_tools/content.py`) used by
  scrape, extract, map, and crawl.
- Crawl runs as an asyncio background job. `start` returns immediately; status,
  paginated results, and cancellation are separate calls.

### Request URL vs page identity

Canonical URLs are used for cache keys, dedupe, and domain scope. They drop
fragments, tracking parameters, default ports, and trailing slashes. Actual
outbound requests use `normalize_request_url`, which preserves the path and
trailing slash: servers commonly redirect `/docs` to `/docs/`, and rewriting the
path inside the redirect loop would repeat forever.

### Scrape flow

1. Validate the URL and canonicalise it for identity; build a separate request URL.
2. Look up `scrape:<canonical>:<max_chars>` in the TTL cache.
3. Fetch over plain HTTP with host/global gates, timeout, size limit, redirect
   validation, retry, and CAPTCHA / access-wall detection.
4. Extract Markdown, title, description, canonical URL, metadata, and links.
5. If the HTML looks like a client-rendered shell, render once in the shared
   headless browser. If rendering fails, keep the usable HTTP content and add a
   warning instead of failing the whole scrape.
6. Hash the extracted Markdown, cache the payload, and return stage timings.

### Discovery depth

`web_search` takes an optional `depth`:

| Depth | Provider queries | Use it for |
|---|---|---|
| `fast` | 1 (`base`) | Low latency, minimal provider load, or when you already know the query |
| `discover` (default) | 3 (`base`, `current`, `background`) | General research; mixes current material with historical context |
| `deep` | 4 (`base`, `current`, `background`, `primary`) | Research that should include official documentation or primary sources |

`current` appends the current year unless the query already contains one;
`background` appends a history/background cue. `primary` asks for official
documentation on technical queries and primary sources otherwise. The suffix is
truncated so the composed query respects the provider length limit, and angles
run sequentially with `SEARCH_ANGLE_DELAY_MS` between them to reduce
rate-limit pressure. `discover` and `deep` request a larger per-angle limit
(`2 × limit`, clamped to 8..`search_max_results`) so ranking has candidates to
choose from; the response is still cut to `limit`.

Results from all angles are canonicalised, deduplicated by URL, and ranked
deterministically:

- lexical overlap with the query (Turkish-aware folding, so `İstanbul` matches
  `Istanbul`) and reciprocal rank fusion across angles;
- source-type authority (`official`, `academic`, `news`, `reference`, `code`,
  `community`, `other`), classified from the host and path;
- publication year inferred from title, snippet, and URL, with `current`,
  `recent`, `historical`, or `unknown` recency;
- a current-year bonus, plus a historical bonus for authoritative sources so
  older primary material is not automatically buried;
- an exact-phrase bonus and a bonus when the matching angle agrees with the
  inferred recency;
- a per-domain cap of `ceil(limit / 3)` and a source-type diversity adjustment,
  so one site or one kind of source cannot fill the page.

Scores are relative ranking weights, not probabilities. Every result carries
`source_type`, `year`, `recency`, `angles`, `relevance`, and `score`. The
response also includes `facets` (source types, recency buckets, domains, angles,
candidate count), per-angle `queries` with result/new counts and errors, and
`warnings`. If an enrichment angle fails, usable results from the remaining
angles are returned with a warning; if every angle fails, the first provider
error is raised. `fast` keeps provider order and does not fan out, so it sends
the fewest requests.

## Configuration

Copy `.env.example` to `.env` and set what you need. The important web tool knobs
(all prefixed `WEB_TOOLS_`):

| Variable | Default | Meaning |
|---|---|---|
| `SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo` or `google` |
| `SEARCH_DEPTH` | `discover` | Default `fast`, `discover`, or `deep` |
| `SEARCH_ANGLE_DELAY_MS` | `150` | Pause between discovery angles (0..5000) |
| `FETCH_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `FETCH_RETRIES` | `1` | Retries for transient failures only |
| `MAX_RESPONSE_BYTES` | `5000000` | Response body cap |
| `MAX_REDIRECTS` | `5` | Redirect hop cap |
| `GLOBAL_CONCURRENCY` | `8` | Whole-process fetch cap |
| `HOST_CONCURRENCY` | `2` | Simultaneous requests per host |
| `HOST_MIN_INTERVAL_MS` | `200` | Minimum delay between starts per host |
| `CACHE_TTL_SECONDS` | `900` | Scrape cache TTL |
| `CACHE_MAX_ENTRIES` | `256` | Scrape cache capacity |
| `BROWSER_ENABLED` | `true` | Enable the headless worker |
| `BROWSER_PREFERENCE` | `auto` | `auto`, `chrome`, `chromium`, or `camoufox` |
| `BROWSER_POST_LOAD_WAIT_MS` | `500` | Settle time after DOM content loaded |
| `BROWSER_MAX_CONTEXTS` | `4` | Concurrent browser contexts |
| `BROWSER_RENDER_MIN_CHARS` | `200` | Text threshold for shell detection |
| `RESPECT_ROBOTS` | `true` | Crawl robots.txt policy |
| `MAX_CRAWL_PAGES` | `100` | Hard page cap |
| `MAX_CRAWL_DEPTH` | `4` | Hard depth cap |
| `CRAWL_CONCURRENCY` | `4` | Pages fetched in parallel by a crawl job |
| `MAX_CRAWL_JOBS` | `32` | In-memory job cap |

`API_WEB_RATE_LIMIT_PER_MINUTE` controls authenticated REST access to the web
routes. Existing general and scrape research limits are unchanged.

## Dot Desktop integration

Dot starts local MCP servers itself over stdio. Add one table to
`~/Library/Application Support/Dot/config.toml` (this repository does **not**
modify Dot's live config):

```toml
[[mcp]]
name = "web-research"
command = "/Users/dogan/Library/Application Support/Dot/workspace/web-research-stack/scripts/web-research-mcp"
args = []
enabled = true
startup_timeout_s = 60.0
call_timeout_s = 300.0
```

No secrets are needed for the default DuckDuckGo provider, so there is no `env`
entry and no API key in Dot's config. If you want to override a non-secret knob:

```toml
env = { WEB_TOOLS_SEARCH_PROVIDER = "duckduckgo", WEB_TOOLS_BROWSER_PREFERENCE = "auto" }
```

Do not put `API_KEYS` or provider credentials in this block. The MCP process is
local and authenticates by being started by Dot. The launcher script picks
`.venv/bin/python` when present, otherwise `python3`, and changes to the project
root so relative imports and `.env` loading work.

Manual steps:

1. Install the project in its virtualenv (see `README.md`).
2. Add the `[[mcp]]` table above to Dot's `config.toml`.
3. Restart Dot, or run `dotx mcp list` to start the server and list its tools.
4. In a Dot conversation, the tools are exposed as
   `mcp__web-research__web_search`, `mcp__web-research__web_scrape`, and so on.
   Dot reads the annotations (`readOnlyHint`, `destructiveHint`, `openWorldHint`)
   when deciding whether to ask for approval.

Example MCP call the model makes (tool arguments follow the listed input schema):

```json
{"name": "mcp__web-research__web_search", "arguments": {"query": "python asyncio", "limit": 5, "depth": "discover"}}
```

The search response includes the ranked `results` plus `facets`, per-angle
`queries`, and `warnings`. Use `"depth": "deep"` when official documentation or
primary sources matter, and `"depth": "fast"` for a single low-latency query.

A scrape follows the same shape with `{"url": "https://docs.python.org/3/library/json.html"}`.
Crawls return a job id immediately; Dot then calls `web_crawl_status`,
`web_crawl_results`, or `web_crawl_cancel` with that id.

### REST examples

```bash
export API_KEY="$(grep -E '^API_KEYS=' .env | cut -d= -f2 | cut -d, -f1)"

curl -sS http://127.0.0.1:8000/api/v1/web/search \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"query": "python asyncio", "limit": 3, "depth": "discover"}'

curl -sS http://127.0.0.1:8000/api/v1/web/scrape \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"url": "https://docs.python.org/3/library/json.html"}'

curl -sS http://127.0.0.1:8000/api/v1/web/crawl \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"url": "https://quotes.toscrape.com/", "options": {"max_pages": 5, "max_depth": 1}}'
```

## Headless browser policy

`web_scraper/web_tools/browser.py` has no code path that launches a visible
window. Chrome is launched with `headless=True`, Playwright's temporary
`--user-data-dir`, `--remote-debugging-pipe`, and `--no-startup-window`.
Camoufox is constructed with `headless=True`. There is no `open -a`, no
minimise-and-hope path, and no silent fallback to a visible browser. If no
headless engine starts, the scrape returns `browser_unavailable` rather than
opening a window.

The legacy research Google fallback in `web_scraper/google_search.py` still has
its older minimise-window path for its own endpoints. The web tools never use it:
`GoogleSearchAdapter` constructs `GoogleSearcher(headless=True,
allow_visible_browser=False)`, and the page fetcher does not call Google at all.

### Verify that HTTP pages never start a browser

The benchmark checks this directly:

```bash
.venv/bin/python scripts/benchmark_web_tools.py
python -c "import json; d=json.load(open('benchmarks/web_tools_benchmark.json')); print(d['http']['browser_verification']); print(d['checks'])"
```

`started_during_http_suite` must be `false` and `browser_absent_during_http`
must be `true`.

### Verify the Chrome process is headless

Run a render in the background, then inspect its command line while it runs:

```bash
.venv/bin/python - <<'PY' &
import asyncio
from web_scraper.web_tools.browser import HeadlessBrowserManager

async def main():
    manager = HeadlessBrowserManager(preference="chrome")
    await manager.render("https://quotes.toscrape.com/js/")
    await asyncio.sleep(10)
    await manager.stop()

asyncio.run(main())
PY

sleep 3
ps -axo pid,command | grep -E '[C]hrome.*(headless|playwright_chromiumdev_profile)' | head -3
wait
```

The output contains `--headless`, a temporary
`--user-data-dir=.../playwright_chromiumdev_profile-...`, and
`--remote-debugging-pipe`. Observed on this Mac for the final verification; no
visible window was opened.

## Security boundaries

- Only `http` and `https` URLs are accepted.
- Credentials in URLs, `localhost`, and non-public IP literals are rejected
  before any request; DNS is resolved for every request and every redirect hop.
- Browser subresources are routed through the same public-target check; non-http
  schemes are aborted.
- Responses are capped by bytes, redirects by count, and requests by timeout.
- Captchas and login/access walls are classified and fail fast. An
  `access_blocked` HTTP response may fall back to one headless render; a captcha
  never opens a browser.
- Crawls respect `robots.txt` by default (cached for one hour), stay on the seed
  host unless subdomains are explicitly allowed, and dedupe by canonical URL and
  extracted-content hash.
- Web tool results and errors do not include request headers, API keys, or
  provider credentials. No secrets are written to the repository.
- Each crawl job runs as a background asyncio task with a job cap, page/depth
  caps, and cancellation of queued and in-flight page tasks.

## Benchmarks

Run:

```bash
.venv/bin/python scripts/benchmark_web_tools.py
```

The benchmark scrapes 20 public pages sequentially with an empty cache (`cold`),
then scrapes them again (`cached`). It also measures four DuckDuckGo queries and
renders a JavaScript page in the shared headless Chrome (one cold start, then
three warm renders with the cache cleared to force the browser path). Raw output:
`benchmarks/web_tools_benchmark.json`.

Final run: 2026-10-22, macOS 26.6.2 arm64, Python 3.13.16, Chrome 155.

| Stage | Samples | P50 | P95 |
|---|---:|---:|---:|
| Search (provider wall) | 4 | 992.67 ms | 1385.97 ms |
| HTTP cold: wall | 20 | 597.86 ms | 952.19 ms |
| HTTP cold: network (final hop) | 20 | 179.25 ms | 713.31 ms |
| HTTP cold: whole fetch (redirects/gates) | 20 | 383.74 ms | 787.26 ms |
| HTTP cold: Markdown extraction | 20 | 119.44 ms | 501.09 ms |
| HTTP cold: app time (`total - http`) | 20 | 120.86 ms | 504.09 ms |
| HTTP cached: wall | 20 | 0.05 ms | 0.35 ms |
| Browser warm: render | 3 | 3451.27 ms | 3524.30 ms |

- HTTP cold succeeded 20/20; cached returned 20/20 hits (cache speedup at P50:
  about 11,957×). A cached hit still pays the public-call wrapper, hence 0.05 ms.
- No browser was started during the HTTP suite (`engine_after=null`,
  `running_after=false`).
- Browser: Chrome, cold start + first render 7,252 ms; warm render P50 3,451 ms
  with the worker reused. The warm cost is dominated by Playwright page creation,
  navigation, and the 500 ms settle, not browser startup.
- Search returned five results for 3 of 4 queries; `sqlite full text search`
  returned zero results without raising. Treat empty results as a provider
  quality signal and retry or choose another provider when discovery matters.
- Network numbers include target and transport latency only. `app` is the
  service/extraction work after the HTTP fetch. These are single-machine
  measurements over the public internet, not an SLA; results vary with the
  targets and the connection.

### Controlled load test

```bash
.venv/bin/python scripts/load_test_web_tools.py
```

The fixture is loopback-only: `.test` hostnames are routed to a local
`ThreadingHTTPServer`, the browser is disabled, and no third-party site receives
load. It runs 60 single-host scrapes, 160 mixed-host scrapes, six 12-page
crawls, four crawl jobs with two cancelled mid-flight, and 32 concurrent stub
searches. Raw output: `benchmarks/web_tools_load_test.json`.

Final run: 2026-10-22, 400 ms fixture latency.

| Phase | Requests | P50 | P95 | Peak concurrent |
|---|---:|---:|---:|---:|
| Single host | 60 | 6,480 ms | 12,138 ms | 2 |
| Eight hosts | 160 | 4,729 ms | 8,519 ms | 8 |
| Crawl (6 jobs) | 72 pages | — | — | 8 |
| Cancel 2 of 4 | — | — | — | — |

All 10 checks passed: no failed scrapes or crawls, per-host peak 2, global peak
8, all 72 crawl pages fetched, cancelled jobs reached `cancelled` in 51 ms, and
the fixture drained in 291 ms. Peak RSS was 59.5 MB; this is a short run, not a
soak test.

The test found a gate-ordering defect: requests waited on the per-host interval
while holding a global slot, so a same-host queue could starve other hosts. A
unit test measured the first request to another host starting at 270 ms; after
the fix the same 160-request, 5 ms fixture moved P95 from 19,270 ms to
3,954 ms.

### Live discovery comparison

Query `python asyncio best practices`, `limit=8`, 2026-10-22, DuckDuckGo:

| Depth | Provider calls | Unique candidates | Results (official / other / community) | Current-year results | Wall |
|---|---:|---:|---|---:|---:|
| `fast` | 1 | 8 | 2 / 4 / 2 | 1 | 1,642 ms |
| `discover` | 3 (2 succeeded) | 26 | 3 / 3 / 2 | 2 | 5,949 ms |
| `deep` | 4 (2 succeeded) | 29 | 3 / 3 / 2 | 3 | 5,305 ms |

`fast` kept provider order, which put a tutorial ahead of the official
`docs.python.org` pages. `discover` and `deep` promoted the official asyncio
documentation to rank 1, kept a current 2026 guide at rank 2, and filled the
page with distinct domains (cap 3). DuckDuckGo bot-detected the `background`
angle in both discovery runs and the `primary` angle in the deep run; the
service returned the successful angles with `background_query_failed` and
`primary_query_failed` warnings instead of failing the search. That is the
designed partial-failure behavior, not a complete four-angle result. When the
provider cooperates, `deep` adds its fourth angle; when it does not, retry later
or use `fast` to conserve provider budget.

## Verification

Checks actually run during this work:

```bash
.venv/bin/python -m pytest -q          # 219 passed
.venv/bin/python -m ruff check .       # All checks passed
.venv/bin/python scripts/benchmark_web_tools.py   # 6/6 checks passed
.venv/bin/python scripts/load_test_web_tools.py   # 10/10 checks passed
```

The live `fast` / `discover` / `deep` comparison above was also run against
DuckDuckGo on 2026-10-22. The full suite count includes the discovery tests in
`tests/test_web_discovery.py`.

The MCP server was also started through Dot's own MCP SDK (`Client(mode="auto")`)
and exercised live: protocol `2025-11-25`, 8 tools, a DuckDuckGo search returning
URLs, an HTTP scrape returning Markdown, and `http://127.0.0.1/admin` rejected
with `unsafe_target`. After the discovery change, the same SDK path was rerun:
8 tools, `web_search.depth` exposed as a `fast|discover|deep` enum with `null`
allowed, and a live `depth="fast"` call returned 3 results plus facets. The Dot
process already running did not need a restart for this check because the SDK
launched a fresh server; restart Dot if you want its long-lived MCP child to
pick up the new schema.

## Known limits

- JavaScript-shell detection is deterministic, not a full browser oracle. A
  client-rendered page whose shell contains more than `BROWSER_RENDER_MIN_CHARS`
  of boilerplate may stay on the HTTP path. Raise or lower that threshold only
  with a benchmark.
- `web_extract` fetches the page fresh; it does not use the scrape cache.
- Crawl jobs and the scrape cache live in one process and are lost on restart.
  REST and MCP have separate caches.
- The default DuckDuckGo path can be rate-limited. A 202 bot-detection response
  is reported as `search_failed` with `detail=bot_detected`; a 200 response with
  no result nodes still returns an empty list, which is a provider quality
  signal. The adapter can be replaced without changing the service contract.
- `discover` and `deep` send 3 and 4 provider queries instead of 1. They cost
  more latency and provider budget than `fast`; use `fast` when that matters.
  An individual angle failure is surfaced in `warnings` and may reduce coverage.
- Recency is inferred from a plausible year in the title, snippet, or URL, not
  from page metadata. Results whose year is not visible are labelled `unknown`
  and receive no current/historical bonus.
- Source type is a host-and-path heuristic. A blog hosted on a vendor domain can
  be classified `official`; the per-domain cap still prevents it from filling the
  page. Scores are relative to the current result set, not calibrated relevance.
- The Google provider remains headless-only and may be refused; it is not a
  guaranteed search fallback.

## Changed files

New:

- `web_scraper/web_tools/` (`urls.py`, `fetcher.py`, `content.py`, `browser.py`,
  `search.py`, `discovery.py`, `robots.py`, `crawl.py`, `service.py`,
  `settings.py`, `metrics.py`, `errors.py`, `__init__.py`)
- `web_scraper/mcp_server.py`
- `scripts/web-research-mcp`, `scripts/benchmark_web_tools.py`,
  `scripts/load_test_web_tools.py`
- `tests/test_web_primitives.py`, `test_web_fetcher.py`, `test_web_service.py`,
  `test_web_crawl.py`, `test_web_browser.py`, `test_web_api.py`,
  `test_web_discovery.py`, `test_mcp_server.py`
- `docs/web-tools.md`, `benchmarks/web_tools_benchmark.json`,
  `benchmarks/web_tools_load_test.json`

Modified:

- `web_scraper/api.py` (web routes, exception mapping, manifest, lifespan cleanup)
- `web_scraper/api_models.py` (request/response models, discovery fields)
- `web_scraper/config.py` (`API_WEB_RATE_LIMIT_PER_MINUTE`)
- `web_scraper/google_search.py` (headless-only override for adapters)
- `web_scraper/duckduckgo_search.py` (snippet spacing, bot-detection handling)
- `tests/test_duckduckgo_search.py`
- `README.md`
- `.env.example`
