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

Freshness is explicit, not assumed. Queries that ask for it (`latest`, `today`,
`this week`, `güncel`, `son dakika`, or the current year) get a provider date
filter (`day`, `week`, `month`, or `year`); the `current` angle always uses
`year`. General queries stay unfiltered so historical material is not discarded.
DuckDuckGo honours the filter with `df`; the legacy Google adapter accepts the
parameter but has no date-filter implementation. Per-angle `queries` reports
include the applied `date_filter`.

Results from all angles are canonicalised, deduplicated by URL, and ranked
deterministically:

- BM25F lexical scoring over separate title (weight 3.0), snippet (1.2), and URL
  (0.5) fields, with per-field length normalisation and term-frequency
  saturation. Document frequency is computed over the candidate pool, so the
  score reflects how distinctive a term is for this search. Turkish letters are
  folded, so `İstanbul` matches `Istanbul`.
- weighted reciprocal-rank fusion across angles: base 1.0, current 0.9, primary
  0.8, background 0.75. Agreement between angles is still valuable, but a hit
  from the historical angle cannot outweigh the exact query as easily.
- source-type authority (`official`, `academic`, `news`, `reference`, `code`,
  `community`, `other`), classified from the host and path;
- publication year inferred from title, snippet, and URL, with `current`,
  `recent`, `historical`, or `unknown` recency;
- a current-year bonus, plus a historical bonus for authoritative sources so
  older primary material is not automatically buried;
- an exact-phrase bonus and a bonus when the matching angle agrees with the
  inferred recency;
- MMR-style near-duplicate suppression: results whose title+snippet Jaccard
  similarity exceeds 0.55 are penalised, so syndicated copies do not occupy
  several slots;
- a per-domain cap of `ceil(limit / 3)` and a source-type diversity adjustment,
  so one site or one kind of source cannot fill the page.

Scores are relative ranking weights, not probabilities. Every result carries
`source_type`, `year`, `recency`, `angles`, `relevance`, `bm25`, and `score`. The
response also includes `facets` (source types, recency buckets, domains, angles,
candidate count), per-angle `queries` with result/new counts, date filters and
errors, `warnings`, and `cache_hit`. If an enrichment angle fails, usable
results from the remaining angles are returned with a warning. Every failed
provider call puts that provider into a cooldown (5 s doubling to 300 s, reset by
a successful call) during which its calls go to the Google fallback; see
"Search provider pacing, fallback, and cooldown" below. `fast` keeps provider
order and does not fan out, so it sends the fewest requests. Successful responses are cached for
`WEB_TOOLS_SEARCH_CACHE_TTL_SECONDS` (default 300 s) per provider/depth/limit/
query/locale, so a repeated search skips the provider entirely.

## Configuration

Copy `.env.example` to `.env` and set what you need. The important web tool knobs
(all prefixed `WEB_TOOLS_`):

| Variable | Default | Meaning |
|---|---|---|
| `SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo`, `duckduckgo_lite` or `google` |
| `SEARCH_DEPTH` | `discover` | Default `fast`, `discover`, or `deep` |
| `SEARCH_ANGLE_DELAY_MS` | `150` | Pause between discovery angles (0..5000) |
| `SEARCH_REGION` | `wt-wt` | DuckDuckGo `kl` region, e.g. `tr-tr`; `wt-wt` is neutral |
| `SEARCH_LANGUAGE` | `en-US,en;q=0.9` | `Accept-Language` value |
| `SEARCH_CACHE_TTL_SECONDS` | `300` | Search response cache TTL |
| `SEARCH_TIMEOUT_SECONDS` | `30` | Per-call provider timeout |
| `SEARCH_PROVIDER_CONCURRENCY` | `1` | Calls one provider takes at a time |
| `SEARCH_JITTER_MIN_MS` / `SEARCH_JITTER_MAX_MS` | `250` / `500` | Random gap between a provider's calls |
| `SEARCH_FALLBACK_PROVIDERS` | `duckduckgo_lite,google` | The chain a failed call goes down, in order; `none` turns it off. The older single `SEARCH_FALLBACK_PROVIDER` is read when this is unset |
| `SEARCH_FALLBACK_TIMEOUT_SECONDS` | `60` | Timeout of a fallback call (a cold headless browser) |
| `SEARCH_PROVIDER_COOLDOWN_SECONDS` | `5` | First cooldown after a failed provider call |
| `SEARCH_PROVIDER_MAX_COOLDOWN_SECONDS` | `300` | Cap for the doubling cooldown |
| `SCRAPE_MAX_LINKS` | `50` | Links `web_scrape` lists, from the page content |
| `ARCHIVE_BUDGET_SECONDS` | `20` | Time the walk through the archive's copies may take (`archive: true`) |
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

The search row predates multi-angle discovery becoming the default: each sample
was one provider query. Scrape and browser rows are unaffected by ranking
changes. Use the live discovery comparison below for per-depth latency and
candidate yield.

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
| `fast` | 1 (1 succeeded) | 8 | 2 / 4 / 2 | 1 | 991 ms |
| `discover` | 3 (2 succeeded) | 31 | 2 / 5 / 1 | 3 | 4,436 ms |
| `deep` | 4 (3 succeeded) | 38 | 3 / 4 / 1 | 3 | 6,362 ms |

`fast` kept provider order, which put a Real Python walkthrough ahead of the
official `docs.python.org` pages. `discover` and `deep` promoted official
material to the top two slots — `discuss.python.org` and `docs.python.org` for
`discover`, both `docs.python.org` pages for `deep` — and placed a 2026 guide at
rank 3 while keeping the page on distinct domains (cap 3). DuckDuckGo
bot-detected the `background` angle in both discovery runs; the service returned
the successful angles with a `background_query_failed` warning instead of
failing the search. The `primary` angle succeeded in the `deep` run. That is the
designed partial-failure behavior, not a complete four-angle result. A repeated
`discover` search returned from cache in 0.35 ms with `cache_hit=true`; a failed
provider call would instead cool that provider down and send its next calls to
the fallback. When the provider
cooperates, `deep` adds its fourth angle; when it does not, retry later or use
`fast` to conserve provider budget.

## Verification

Checks actually run during this work:

```bash
.venv/bin/python -m pytest -q          # 234 passed
.venv/bin/python -m ruff check web_scraper tests scripts   # All checks passed
.venv/bin/python -m ruff format --check web_scraper tests  # 60 files already formatted
.venv/bin/python scripts/benchmark_web_tools.py   # 6/6 checks passed
.venv/bin/python scripts/load_test_web_tools.py   # 10/10 checks passed
```

The live `fast` / `discover` / `deep` comparison above was also run against
DuckDuckGo on 2026-10-22. The full suite count includes the discovery, search
cache, cooldown, timeout, locale, and BM25F tests in `tests/test_web_discovery.py`
and `tests/test_web_service.py`.

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
- Crawl jobs and the scrape/search caches live in one process and are lost on
  restart. REST and MCP have separate caches. Provider cooldown state is also
  per process.
- The default DuckDuckGo path can be rate-limited. A 202 bot-detection response
  is reported as `search_failed` with `detail=bot_detected`; a 200 response with
  no result nodes still returns an empty list, which is a provider quality
  signal. Every failed call cools the provider down and sends its next calls to
  the Google fallback. The adapter can be replaced without changing the service
  contract.
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

## Reliability fixes (2026-10-22)

Behavior changes from live testing of the tools. The unit tests never reach the
network; `scripts/verify_web_tools_live.py` runs the live checks by hand
(`--only scrape,pdf,search,empty,crawl,links`, or `--only matrix --urls FILE`
to scrape a list of URLs and count blocks). On a 29-URL list of news, social,
Finnish public, and test sites, scrapes that succeeded went from 14 to 18; the
four captchas left (Reddit, Reuters, NYT, WSJ) are real challenge pages, which a
headless browser met as well.

### Challenge detection

- A captcha is reported only on evidence a visitor would meet: a widget in the
  DOM (`g-recaptcha`, `h-captcha`, `cf-turnstile`, `px-captcha`), a captcha
  iframe, a form that posts to a captcha endpoint, or a challenge phrase
  (`unusual traffic`, `verify you are human`, `are you a robot`) in the visible
  text with scripts, styles, and templates removed. The word `captcha` inside a
  script no longer counts; it made Wikipedia, GitHub, Instagram, and
  Verkkokauppa fail as captchas.
- A 2xx response over 100 KB is content, never a captcha, with one exception: a
  page with at most 3,000 visible characters that carries a captcha form.
  Reddit's "Prove your humanity" page is 167 KB of inline assets around one, and
  it is reported as `captcha` rather than handed over as the subreddit.
- Challenge, block, and wall phrases count only on pages with at most 3,000
  visible characters, so an article about "access denied" errors is content.
- Invisible reCAPTCHA (the v3 badge, `data-size="invisible"`) is not a challenge.
- A 401/403 HTML page of 20 KB or less is `access_blocked`, which may fall back
  to one headless render; a JSON 401/403 stays an `http_error`. When that render
  fails, the error is the HTTP refusal with its status (not the browser's
  `navigation_failed`); a challenge the browser meets carries the browser's
  status. DataDome sites (Reuters, NYT, WSJ) take this path: their 401/403 page
  names the captcha only in a script, and the rendered page shows its iframe.
- A login wall needs a login title plus a login path or a password field; the
  word "password" in scripts no longer counts.

### PDF documents

- `web_scrape` reads PDFs instead of refusing them as `unsupported_content_type`.
  A response is a PDF when it says `application/pdf` (or `application/x-pdf`) or
  its body starts with `%PDF-`. The download stays inside `MAX_RESPONSE_BYTES`.
- pypdf extracts the text off the event loop, page by page, into Markdown with a
  `## Page N` section per page, until `max_chars` is reached (at most 500 pages
  are read). The response carries `content_type: "application/pdf"`, `pages`
  (the document's full page count), the PDF's title when its metadata has a
  real one, and its author, dates, and producer under `metadata.pdf`.
- Warnings: `pdf_truncated` when pages were left unread, `pdf_no_text` for a
  scanned PDF without a text layer.
- Errors: `pdf_encrypted` for a PDF that needs a user password (one with only an
  owner password opens normally; AES needs the optional `cryptography`
  package, which this environment does not install), `pdf_invalid` for one
  that cannot be parsed, and `timeout` (`pdf_extract_timeout`) when extraction
  takes longer than `FETCH_TIMEOUT_SECONDS`. Both PDF codes map to HTTP 422.

### Search provider pacing, fallback, and cooldown

- Each provider takes one call at a time (`SEARCH_PROVIDER_CONCURRENCY`, default
  1), shared by every search and angle in the process, with a random 250–500 ms
  gap between calls (`SEARCH_JITTER_MIN_MS`, `SEARCH_JITTER_MAX_MS`). Each call
  is bounded by `SEARCH_TIMEOUT_SECONDS` (30), a fallback call by
  `SEARCH_FALLBACK_TIMEOUT_SECONDS` (60): the headless Google browser's first
  search took 36 s in testing.
- A failed call (`bot_detected`, a timeout, or another provider error) is asked
  of the fallback chain (`SEARCH_FALLBACK_PROVIDERS`; before round 3 one
  `SEARCH_FALLBACK_PROVIDER`, default `google`), see "Search that does not fall
  to the browser every time" below. `provider` names who answered (`google`, or
  `duckduckgo+google` when the angles were split), `requested_provider` keeps
  the requested one, the angle's entry in `queries` carries
  `fallback: {from, to, reason, error}`, and `warnings` gets `provider_fallback`.
- A failure cools down only the provider that failed: 5 s
  (`SEARCH_PROVIDER_COOLDOWN_SECONDS`), doubling while failures repeat up to
  300 s (`SEARCH_PROVIDER_MAX_COOLDOWN_SECONDS`); a success resets it. While it
  cools down, its calls go to a healthy fallback (`reason: provider_cooldown`).
  With no fallback, or when the fallback fails too, the provider is still
  asked: a cooldown steers calls and never fails a search by itself.
  `GET /api/v1/web/stats` lists each provider's failures and remaining cooldown.
- Measured on 2026-10-22: six `discover` searches sent in parallel each
  returned 5 results with no failed query. DuckDuckGo still refused 2 of the 10
  calls it got at this pace; Google answered those two and the 8 angles sent
  while DuckDuckGo cooled down. Asked one query at a time with 3 s pauses,
  DuckDuckGo answered every call, so a wider jitter trades latency for fewer
  refusals.
- The Google fallback is the headless `GoogleSearchAdapter` (Camoufox): its
  first call starts the browser (6–36 s measured), which holds memory until it
  has been idle for three minutes.

### Honest empty searches

- A result that shares no word with the query is dropped at every depth. When
  nothing relevant is left, `results` is empty and `warnings` holds
  `no_relevant_results`; the old fallback returned five unrelated results
  (relevance 0.0) for `qzxvbnmp flurbulator 9237`. A bare number (a year, a
  code like `9237`) does not count as a shared word when the query has words.
- A provider that answers with no results at all gives the same empty answer
  instead of a `search_failed` error. `facets.total_candidates` still counts
  what the provider returned.
- In `discover` and `deep`, a relevant result scoring under 0.25 is dropped
  beside better ones and kept only when no relevant result reaches 0.25. When
  every returned result holds under half of the query's words (30% before
  round 3), `warnings` holds `low_relevance_results`. `fast` keeps the
  provider's order.

### Official and academic sources

- `official` now also covers every host under `europa.eu`, `gov.uk` and any
  `gov.<country>` (`gov.tr`, `gov.au`), `.gov`, `.mil`, and `.int` (checked
  before every other class), and a curated list of Finnish public bodies:
  `kela.fi`, `traficom.fi`, `stat.fi`, `vero.fi`, `dvv.fi`, `eduskunta.fi`,
  `oamk.fi`, `yle.fi`, and `ouka.fi` (the City of Oulu).
- `academic` now also covers `.edu.<country>` and `.ac.<country>` (`edu.tr`,
  `ac.jp`, `ac.uk`) and the Finnish universities `oulu.fi` (University of Oulu),
  `helsinki.fi`, `aalto.fi`, `utu.fi`, `jyu.fi`, `tuni.fi`, `uwasa.fi`,
  `abo.fi`, `lut.fi`, and `lappeenranta.fi`.
- Commercial compliance and consultancy sites on other `.eu` hosts
  (`regulation-ai.eu`, `artificialintelligenceact.eu`) stay `other` with no
  authority weight, so the European Commission's AI Act timeline outranks a
  blog about it.

### Crawl schemes

- A crawl runs on https: the seed and every discovered `http://` link on a
  default port are rewritten to `https://`, and a page is queued once per host
  and path whichever scheme linked to it.
- quotes.toscrape.com answers `https://…/author/Albert-Einstein` with a 308 to
  `http://…/author/Albert-Einstein/`, so most results came back as `http://`
  with `http://` links, and one page could enter the results twice. A page an
  https request reached through such a same-host downgrade is now read as its
  https self: its URL and links stay https, and the same content hashes the
  same whichever scheme reached it, so content-hash dedupe catches it. A
  30-page crawl of quotes.toscrape.com now returns no `http://` URL; before
  the change 28 of 30 were `http://`.
- A host that refuses https (a network error on a rewritten URL) is crawled
  over http for the rest of the job.

### Scrape output hygiene

- `web_scrape` lists at most 50 links (`SCRAPE_MAX_LINKS`), taken from the page
  content: links inside `nav`, `header`, `footer`, and `aside` elements and their
  ARIA landmarks (`navigation`, `banner`, `contentinfo`, `complementary`,
  `search`) are left out. `links_truncated` is `true` when the page has links
  the list leaves out, menus included. `web_map` lists them all, and crawls
  still follow menu links (pagination often sits in a `nav`).
- The Markdown also drops the ARIA landmark versions of that chrome, as it
  already dropped `nav`, `header`, `footer`, and `aside` elements.
- Measured on ouka.fi: the front page's links went from 102 entries (12.9 KB)
  to 45 (7.5 KB) and its response from 24.8 KB to 19.4 KB;
  `/tietoja-ja-tilastoja` went from 15.8 KB to 9.9 KB, and to 6.2 KB with
  `max_chars=1500`. What is left is page content: those 45 front-page links are
  its news, topic, and service links, and the rest is its Markdown.

## Defect fixes (2026-10-23)

### DNS failures

- A host that does not resolve now fails with `dns_error` (HTTP 502), its name
  in `detail` and the message `Could not resolve host <name>`; before, it was an
  `unsafe_target` (400), which told the caller the URL was refused rather than
  unreachable. localhost, private and link-local addresses, and a name that
  resolves to one, stay `unsafe_target`.

### Map of an unreachable site

- `web_map` lists the base URL once its page was read. A site no request
  reached (`https://this-domain-definitely-does-not-exist-xyz123.com/`) returned
  that URL with `total: 1`; it now returns `urls: []`, `total: 0`, and the three
  failed requests (robots.txt, sitemap.xml, the page) in `errors`.
- `total` counts every URL that matched the scope and filters before `limit`
  cut the list; `returned` counts `urls`, and `truncated` is `true` when the
  limit left some out. Before, `total` was the length of the cut list.

### Cache hits time themselves

- A `web_scrape` or `web_search` answered from the cache reported the timings
  of the call that filled it (a repeated scrape said `total_ms: 903` and took
  under a millisecond). Its `timings` now hold only `cache_lookup_ms` and the
  real `total_ms`; the stored call's timings move to `cached_timings`, and
  `cached_at` says when it was made (UTC), so a caller can judge its age.

### One minimum for `max_chars`

- The scrape schema accepted `max_chars` from 1 while the service raised any
  value under 500 to 500, so `max_chars: 1` returned 500 characters. The schema
  now states the minimum (`minimum: 500`), a smaller value is refused (HTTP 422
  on REST, `invalid_arguments` on MCP, `invalid_request` from the service), and
  the server's cap (`WEB_TOOLS_MARKDOWN_MAX_CHARS`, 50,000 by default) still
  applies above it.

### Bounded extraction

- `web_extract` had no bound: `{"links": {"selector": "a", "attribute": "href",
  "all": true}}` on the Wikipedia article on the Eiffel Tower answered 114,465
  characters. A field with `all: true` now returns at most `max_items` values
  (100 by default, up to 1,000, `WEB_TOOLS_EXTRACT_MAX_ITEMS`), and its
  provenance holds `total` (every match), `returned`, and `truncated`.
- The whole answer stays under `max_chars` (20,000 characters of compact JSON by
  default, 1,000 to 200,000, `WEB_TOOLS_EXTRACT_MAX_CHARS`): the largest field is
  cut first (a list from its end, a text at its tail with `…`), then evidence;
  the field's provenance says `truncated`, and so does the answer's
  `truncated`. The request schema now describes a field object (`selector`,
  `fallback_selectors`, `attribute`, `all`, `max_items`, `type`, `required`).

### Angles run together after the base

- `discover` and `deep` asked their angles one after another, with a 150 ms
  pause between them. The base angle still goes first (a refusal it meets
  starts the provider's cooldown before the others ask); the angles after it
  now start together, `SEARCH_ANGLE_DELAY_MS` apart, and each waits for the
  provider's gate, so `SEARCH_PROVIDER_CONCURRENCY` and the jitter still decide
  how many calls reach a provider at once. Their outcomes are merged in angle
  order whichever answers first, so `queries`, the `angle_*_ms` timings and the
  ranking are the same as before for the same answers.
- With the default concurrency of 1 the calls still reach DuckDuckGo one at a
  time (it refuses overlapping ones), so a healthy search takes as long as
  before: `AI news October 2026` at `deep` took 4.66 s before and 4.68 s after,
  measured back to back on 2026-10-23. A concurrency of 2 or more lets the
  angles after the base overlap. What made searches take 12–45 s was a refusal
  sending angles to the headless Google fallback (43 s for one base angle in
  the same measurement); the next section bounds that.

### A time budget for the angles after the base

- Once the base angle has answered, the angles after it have
  `SEARCH_ANGLE_BUDGET_MS` (5,000 by default; `0` waits for every angle). One
  still out then is cut: its entry in `queries` holds the error `timeout` with
  the detail `angle_budget`, `warnings` gets `<angle>_query_failed`, and the
  results of the angles that answered are ranked as usual. A cut is the
  search's own limit, so it starts no cooldown for the provider. The base angle
  is never cut.
- A refused angle waits out the cooldown or starts the headless Google
  fallback (6–43 s measured), and the base's answer waited for it. Measured on
  2026-10-23 with `AI news October 2026` at `deep`: 5.06 s when DuckDuckGo
  answered every angle, and 6.81 s when it refused the primary angle, which
  was cut at 5 s; the same refusal took 22.03 s, and 45.67 s on the base,
  without the budget.

### Small fixes

- A sitemap or feed (`application/xml`, `text/xml`, RSS, Atom) is checked for a
  challenge with the XML parser: the HTML parser misread it and warned
  (`XMLParsedAsHTMLWarning`) on every `web_map`. lxml resolves internal
  entities only, so an external entity in a hostile sitemap reads no file.
- A challenge error reads `Target returned an access_blocked interstitial`
  (it said "a access_blocked"); `a captcha` and `an access_wall` follow the
  same rule.

### Fields by name, dates in ISO 8601

- A `web_extract` field asked in plain words came back `null`: `headline`,
  `author` and `published_at` found nothing unless a CSS selector named them,
  and a page's JSON-LD was read for a few names only (an author list came back
  as a Python repr). A field with no selector, or a well-known one whose
  selectors find nothing, is now read by its name (`fields.py`): JSON-LD first
  (an article's item before the site's, `@id` references followed), then meta
  tags (`article:published_time`, `og:site_name`, `author`, ...), then the HTML
  conventions (`<time datetime>`, `rel=author`, `itemprop`, the first `h1`).
  The names cover the headline and title, author (byline, writer),
  published and modified dates (`published_at`, `datePublished`, `date`, ...),
  description, image, site name (publisher), section, keywords (tags),
  language, canonical URL, `lead` (the article's first paragraph of twelve
  words or more, past labels such as "In this article", tables of contents,
  photo captions and carousels: AP News put a caption first), body, word count,
  name, and price. Provenance says which source answered. A name it does not
  know is read from JSON-LD and meta tags under that name alone, and is
  otherwise reported missing.
- Dates read by name, and any field of `type: "date"`, come back in ISO 8601:
  `Oct 23, 2026 8:00 AM` is `2026-10-23T08:00:00`, `Fri, 23 Oct 2026 08:00:00
  GMT` is `2026-10-23T08:00:00+00:00`; one that cannot be read stays as
  written. A selected element's text keeps a space between its parts, so two
  spans no longer read `Oct 23, 20268:00 AM` (nor `Link , more` the other
  way), and a date-typed field prefers the element's `datetime`.
- Measured live: the Wikipedia article on the Eiffel Tower answers
  `published_at` `2001-03-01T20:20:29+00:00`, `modified_at`, `publisher`
  `Wikimedia Foundation, Inc.` and its first paragraph as `lead`; an AP News
  article answers its headline, author, ISO dates, `AP News` and `Business`
  from JSON-LD, and its opening paragraph as `lead`.

### How old a result is, and what kind of page

- `recency` goes by the newest year a result names, so in October a February
  article was `current`, and an agent read that as fresh. Each result now also
  carries `date` (`YYYY-MM-DD`, or `YYYY-MM` for a month), read from its
  address (`/2026/10/20/`, `-2026-10-20`, `/archives/2026/10`), its snippet (a
  date in words, or the provider's "3 days ago" and "Yesterday" at its head)
  or its title, in that order, with `date_source` and `age_days`; `freshness`
  (`this_week`, `this_month`, `this_year`, `older`, `undated`) reads that date
  first and the year only without it. A date after tomorrow is none. `recency`
  is unchanged, and its description now says it goes by the year.
- `kind` says what the address shows: `article` (a slug of four words or
  more, filler words left out, a dated path, an id, or `/article/`, `/wiki/`,
  `/story/`), `listing` (a section, tag, archive, author or search page, or
  `news.html`), `front` (a home page), or `page` when it cannot tell.
- A news query (a freshness cue such as "this week", "latest", "today", or
  the word "news") ranks articles above listings and front pages, and when the
  query says how fresh and a result's date can answer, the date decides: a
  result inside the window gains, one outside it loses more, and the
  year-based bonus is left out for it. Facets count `kinds` and `freshness`.
- Measured live on 2026-10-23 with `AI news this week` at `discover`: the
  first run with the kind alone put a CNN article of 28 February first; with
  the date deciding, it fell to sixth, under a page dated that day.

### What to try after a wall

- A page behind a captcha or a refusal (Reuters answers HTTP 401 with a
  captcha) came back as an error and nothing else. A `blocked`, `http_error`,
  `timeout` or `network_error` from `web_scrape` now carries a `hint`, the one
  field the error envelope gains, and only when there is something to try:
  ask again with `archive: true`, or find the story elsewhere with
  `web_search`. A `dns_error` or an `unsafe_target` has none.
- `archive: true` (off by default) reads the Internet Archive's latest copy
  when the site would not serve the page (round 3 replaced the lookup below with
  the CDX index, see "Archive copies that are not themselves captchas"): its
  availability API, asked for the
  copy closest to today (without a date it named one a year old), names the
  snapshot, and the `id_` form of the snapshot's address serves the page as
  archived, without the archive's banner. The page's links are read against
  its own address, not the archive's, and `url` is the page's. The answer says so:
  `fetched_with: "archive"`, `archived_at` (UTC), `archive_url`, and the
  warning `archived_copy`; it may be days old. A page that answers is read
  live as before. With no copy the site's error stands, its hint saying so,
  and an archive that does not answer is told apart from one with no copy.
- Measured on 2026-10-23: Reuters' AI section answered `blocked`/`captcha`
  with the hint, and with `archive: true` its archived copy (2,324 characters,
  its links on reuters.com). The newest copy the archive holds is of
  2025-09-25, which `archived_at` says: Reuters seems to wall off the
  archive's crawler too, so a caller should read that date. archive.org
  answered HTTP 429 for a while that morning (curl's requests too); that
  failure is told apart from no copy.

### Bounded metadata

- A scrape's `metadata` carried every meta tag and up to 20 JSON-LD items
  whole, so a page could make its answer as large as it liked: an
  `articleBody` repeats the text the Markdown already holds, and a catalogue
  page embeds its products. The Markdown and the links were bounded; this was
  not, and one scrape answered 912,000 characters on one line. `metadata` now
  keeps at most 500 characters of any text and 20 items of any list, the meta
  tags while they fill 5,000 characters and the JSON-LD items while the whole
  fits 10,000, in that shape; `metadata_truncated` says when anything was
  cut. `web_extract` still reads the page's whole JSON-LD.
- Measured live: an AP News article's metadata went from 8,660 characters to
  7,605 and its answer from 26,603 to 25,575; a page of ordinary size keeps
  its metadata as it was (`metadata_truncated: false`), and the bound is for
  the pages that embed far more.

## Resilience and ergonomics (round 3, 2026-10-23)

### A floor under relevance

- `zlorgfimble quaxotronic 7g3` answered eight results from DuckDuckGo and kept
  three, each with relevance 1/3 (above the old floor of 0.3) and no warning: a
  YouTube channel named `7g3`, an antibody clone called 7G3, a page of links.
  They held nothing of the query but the 3-character code `7g3`, which names a
  handle, a clone or a model as readily as a topic. The other two words, the
  ones that say what was asked, were in no result.
- A result is relevant only when it holds one of the query's distinctive terms
  as a whole word: a joined token (`next.js`) or a word between separators
  (`Meow-7g3` holds `7g3`, `sqlite-bm25-search` holds `sqlite` and `bm25`), never
  a piece of a longer word (`a7g3b9`, `google` for `go`). The words an address
  spells count (`kela.fi/housing-allowance`); its ids do not: a query string, a
  numbered segment, a handle (`/@7g3_na`).
- Distinctive terms are the query's words minus stopwords, bare numbers (a
  year, `9237`, a version such as `3.12`) and short codes (3 characters or
  fewer mixing letters and digits: `7g3`, `b12`, `mp3`), when the query has
  other words. Numbers and codes still count towards `relevance`, the share of
  the query's words a result holds; they never make a result relevant alone. A
  query made of nothing else keeps them: `7g3` finds what holds `7g3`, and
  `7g3 antibody` keeps the antibody's page and drops the channel.
- A query whose distinctive terms no result holds answers `results: []` and
  `no_relevant_results`, at every depth, `fast` included. The nonsense query
  above now answers nothing.
- With two or more words, when every result holds under half of them,
  `warnings` holds `low_relevance_results` and the results stay. The floor was
  0.3 of the words, which a result matching one word of three (0.33) passed.
- `relevance` of a real result can only rise: words joined by a hyphen, a slash
  or an underscore now match (`sqlite-bm25-search` held neither `sqlite` nor
  `bm25` before). The recorded top results of the ten queries in
  `benchmarks/search_relevance_queries.json` all keep their results and their
  warnings stay empty (`tests/test_web_search_floor.py`).

### Search that does not fall to the browser every time

- A refused DuckDuckGo search went to the headless Google, which takes 6 s to
  tens of seconds when cold (one `fast` call took 12.8 s, a `deep` one 6-7 s).
  The fallback is now a chain, `SEARCH_FALLBACK_PROVIDERS`, `duckduckgo_lite,google`
  by default: `duckduckgo_lite` is DuckDuckGo's lite page
  (`https://lite.duckduckgo.com/lite/`, a table of results instead of divs) as a
  provider of its own, over plain HTTP, and Google comes after it. Each provider
  keeps its own gate (`SEARCH_PROVIDER_CONCURRENCY`, the jitter) and its own
  cooldown, a name without an adapter is left out, and `provider` may name
  `duckduckgo_lite` too.
- A call walks the chain: the primary (skipped, with no call, while it cools down
  and a fallback is healthy), then each fallback in order, skipping the cooling
  ones, until one answers. If every fallback fails, the primary is still asked
  when it was skipped: a cooldown steers calls, it never fails a search.
- `queries[].fallback` says who was asked and why each was left: `from` (the
  primary), `to` (who answered, or the last asked), `reason` (why the primary was
  left: `bot_detected`, `search_timeout`, `provider_cooldown`, ...), `error` (the
  error of `to` when it failed too) and `tried`, in order, each
  `{"provider", "status": "answered" | "failed" | "skipped", "reason"}`.
- `depth: fast` asks the plain-HTTP providers before a headless browser, whatever
  the configured order, and never waits for a cooldown to end: a cooling
  provider is skipped, and the browser is asked only after every HTTP provider
  failed or is cooling. An adapter says it drives a browser with `headless = True`
  (the Google one does).
- Why DuckDuckGo called the searches bots, measured on 2026-10-23 (seven calls):
  a lite POST of `q` and `kl` alone was answered HTTP 202 with a duck-picking
  challenge, twice, the second time after loading the lite form page first; the
  html endpoint's GET of `q` and `kl` alone, without the `vqd` token the old code
  fetched from `duckduckgo.com` first, was refused the same way; the same
  searches posted with the fields DuckDuckGo's own forms carry (`s=0`,
  `o=json`, `api=d.js`, `vqd=`, `kl`, `bing_market`) were answered 200 with
  results, in one request. The old client also sent a random user agent (Chrome,
  Firefox, Windows, Linux) and `sec-ch-ua` hints of another version and system
  over a TLS fingerprint of a third (curl_cffi's `impersonate`, drawn at random per
  search), made a new client without cookies for every search, and asked
  `duckduckgo.com` for a token before each one. Now a search is one POST of the
  form fields from one long-lived client (per event loop) that impersonates one
  Chrome, keeps DuckDuckGo's cookies and sends that browser's own headers, none
  overridden (only `Accept-Language`, `Origin`, `Referer` and `Sec-Fetch-Site`
  are ours, as a form submit sets them); the next page is the form DuckDuckGo
  returns, posted back. A challenge is still `search_failed` with
  `detail=bot_detected`. Whether this lowers the refusal rate cannot be shown
  with a few calls: a refusal depends on the address and the pace as well.

### Archive copies that are not themselves captchas

- `archive: true` asked the archive's availability API for the closest copy and
  read only that one. On WSJ, NYT and Reuters the closest copy is the captcha
  the archive's crawler met: none of three tries read a page. It now asks the
  CDX index for the last copies that answered 200, one per content digest
  (`https://web.archive.org/cdx/search/cdx?url=...&output=json&filter=statuscode:200&collapse=digest&limit=-8&fl=timestamp,original,statuscode,digest`),
  and reads them newest first (the `id_` address of each, under the address it
  was archived at), at most four, within `ARCHIVE_BUDGET_SECONDS` (20 by
  default). The first copy that reads is the answer, and `archived_at` is that
  copy's date, not the newest one's.
- A copy the existing challenge check flags (a captcha, a login wall, an
  `access_blocked` page) or that has no text to read is skipped and listed in
  `archive_skipped`, newest first, each `{"timestamp", "reason"}` (`captcha`,
  `access_wall`, `access_blocked`, `empty_content`, or the error's code). It is
  `[]` when the newest copy read, and absent when the archive was not used. No
  headless browser is opened for an archived copy.
- When no copy reads, the site's error stands and its hint says which: the
  archive holds no copy; its newest copies are walls too (their dates and
  reasons); it answered HTTP 429, with the advice to try again later (on the
  lookup or on a copy, which ends the walk); it did not answer; or the walk ran
  past the time budget.
- archive.today (`https://archive.ph/newest/<url>`) is left out. One request
  from this Mac on 2026-10-23, with the fetcher's own user agent, got no
  answer: the connection timed out after 25 s, so it serves this tool no page,
  readable or not. It is also known to put a captcha in front of automated
  clients, which the tool must not try to pass. Add it as a second source only
  where it answers a script a readable page.

### The version, and which code runs

- The version is `1.2.0` (`pyproject.toml`, `web_scraper/__init__.py`).
- The MCP server's `serverInfo.version` carries the commit when the package
  runs from a git checkout: `1.2.0+g727ec4f` (a worktree counts). It is read
  once, at start, with `git rev-parse --short HEAD` given one second; without a
  checkout, or when git is missing, slow or fails, it is the plain `1.2.0`.
- At start the server writes one line to stderr, which Dot keeps in
  `~/Library/Logs/Dot/mcp-web-research.log`, so the owner can see which code is
  live:
  `web-research mcp server started: version=1.2.0+g727ec4f commit=727ec4f python=3.13.16`
  (`commit=unknown` without one).
- The `XMLParsedAsHTMLWarning` that filled that log came from every path that
  reads a document with the HTML parser: scraping or extracting from a feed, a
  sitemap served as `text/html`, a challenge check with no content type. A
  document that opens with an XML declaration and is not XHTML is now parsed with
  the XML parser whatever type it was sent as (an XHTML page stays a web page),
  so no warning is raised; a feed still reads, its `title` and items by selector.

