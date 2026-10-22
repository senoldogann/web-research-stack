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
| `SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo` or `google` |
| `SEARCH_DEPTH` | `discover` | Default `fast`, `discover`, or `deep` |
| `SEARCH_ANGLE_DELAY_MS` | `150` | Pause between discovery angles (0..5000) |
| `SEARCH_REGION` | `wt-wt` | DuckDuckGo `kl` region, e.g. `tr-tr`; `wt-wt` is neutral |
| `SEARCH_LANGUAGE` | `en-US,en;q=0.9` | `Accept-Language` value |
| `SEARCH_CACHE_TTL_SECONDS` | `300` | Search response cache TTL |
| `SEARCH_TIMEOUT_SECONDS` | `30` | Per-call provider timeout |
| `SEARCH_PROVIDER_CONCURRENCY` | `1` | Calls one provider takes at a time |
| `SEARCH_JITTER_MIN_MS` / `SEARCH_JITTER_MAX_MS` | `250` / `500` | Random gap between a provider's calls |
| `SEARCH_FALLBACK_PROVIDER` | `google` | Asked once when a call fails; `none` turns it off |
| `SEARCH_FALLBACK_TIMEOUT_SECONDS` | `60` | Timeout of a fallback call (a cold headless browser) |
| `SEARCH_PROVIDER_COOLDOWN_SECONDS` | `5` | First cooldown after a failed provider call |
| `SEARCH_PROVIDER_MAX_COOLDOWN_SECONDS` | `300` | Cap for the doubling cooldown |
| `SCRAPE_MAX_LINKS` | `50` | Links `web_scrape` lists, from the page content |
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
  once of the fallback provider (`SEARCH_FALLBACK_PROVIDER`, default `google`;
  `none` turns it off). `provider` names who answered (`google`, or
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
  every returned result names less than 30% of the query's words, `warnings`
  holds `low_relevance_results`. `fast` keeps the provider's order.

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
