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
| Providers' state | `GET /api/v1/web/health` | `web_health` (`mcp__web-research__web_health`) |
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
- `SafeFetcher` is HTTP-first. It validates every redirect hop, opens its
  connections through a pinned transport (a host name is resolved once, inside
  the connection; every address must be public; the connection goes to one of
  them: see [Security boundaries](#security-boundaries)), caps response bytes,
  retries transient failures with exponential backoff, and applies global and
  per-host gates.
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
5. If the HTML looks like a client-rendered shell (under `BROWSER_RENDER_MIN_CHARS`
   characters of readable text while the HTML has scripts), read it again in the
   shared headless browser, which waits for the page to settle
   ([Pages drawn by scripts](#pages-drawn-by-scripts-are-waited-for)). If rendering
   fails, keep the usable HTTP content and add a warning instead of failing the whole
   scrape; a page that is still a shell says so (`rendered_shell`, `needs_javascript`).
6. A page that does not name itself (no canonical link to its own address) may be what
   its site answers any address with: it is compared with the host's answer to a path
   that cannot exist ([Pages that do not exist](#pages-that-do-not-exist-soft-404)).
7. Hash the extracted Markdown, cache the payload, and return stage timings.

Scrape, extract, a crawl's pages and a map's start page all read a page through this one
loader (`WebToolsService._load_page`), so that two tools never say different things of one page.

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
- source-type authority (`official`, `academic`, `news`, `press_release`, `social`,
  `reference`, `code`, `community`, `aggregator`, `other`), classified from the host and path
  and, for a host a query names, the query ([How each source type is decided](#how-each-source-type-is-decided));
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

Scores are ranking weights, not probabilities. `bm25` is squashed into 0..1
(`raw / (raw + 1)`), not scaled to the best match of the search: a pool of only
weak matches does not look as strong as a pool with a genuine answer, and one page
that spells the whole query does not take all of BM25's weight from an official
page whose title holds fewer of the query's words. Whether a result is about the
query at all is `relevance`, the share of the query's words it holds (see "A
floor under relevance" below). Every result carries `source_type`, `year`,
`recency`, `angles`, `relevance`, `bm25`, and `score`. The response also includes
`facets` (source types, recency buckets, domains, angles, candidate count),
per-angle `queries` with provider, result/new counts, date filters and errors,
`warnings`, and `cache_hit`. If an angle fails, usable results from the remaining
angles are returned with a warning. Each provider is paced, a wall (or the third failure
in a row) earns it a cooldown of 10 minutes (since 1.3.0; it was 5 s doubling to 300 s), and the
fallback chain answers while it rests (see
[Search provider pacing, fallback, and cooldown](#search-provider-pacing-fallback-and-cooldown)
and [Providers that heal](#providers-that-heal)).
`fast` keeps provider order and does not fan out, so it sends the fewest
requests. Successful responses are cached for
`WEB_TOOLS_SEARCH_CACHE_TTL_SECONDS` (default 300 s) per provider/depth/limit/
query/locale, so a repeated search skips the provider entirely.

## Configuration

Copy `.env.example` to `.env` and set what you need. The important web tool knobs
(all prefixed `WEB_TOOLS_`):

| Variable | Default | Meaning |
|---|---|---|
| `SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo`, `duckduckgo_lite`, `yahoo` or `google` |
| `SEARCH_DEPTH` | `discover` | Default `fast`, `discover`, or `deep` |
| `SEARCH_ANGLE_DELAY_MS` | `150` | Pause between discovery angles (0..5000) |
| `SEARCH_ANGLE_BUDGET_MS` | `5000` | How long the angles after the base may take once it answered (0..120000; `0` waits for every angle); one still out is cut, see "A cut angle says so" |
| `SEARCH_REGION` | `wt-wt` | DuckDuckGo `kl` region, e.g. `tr-tr`; `wt-wt` is neutral. A request's `region` replaces it |
| `SEARCH_LANGUAGE` | `en-US,en;q=0.9` | `Accept-Language` value. A request's `language` replaces it |
| `SEARCH_CACHE_TTL_SECONDS` | `300` | Search response cache TTL |
| `SEARCH_TIMEOUT_SECONDS` | `30` | Per-call provider timeout |
| `SEARCH_PROVIDER_CONCURRENCY` | `1` | Calls one provider takes at a time |
| `SEARCH_JITTER_MIN_MS` / `SEARCH_JITTER_MAX_MS` | `250` / `500` | Random gap between a provider's calls |
| `SEARCH_FALLBACK_PROVIDERS` | `duckduckgo_lite,yahoo,google` | The chain a failed call goes down, in order; `none` turns it off. The older single `SEARCH_FALLBACK_PROVIDER` is read when this is unset |
| `SEARCH_FALLBACK_TIMEOUT_SECONDS` | `60` | Timeout of a fallback call (a cold headless browser) |
| `SEARCH_PROVIDER_COOLDOWN_SECONDS` | `600` | The cooldown a wall, or the `SEARCH_PROVIDER_FAILURE_LIMIT`th failure in a row, starts (10 minutes; it was 5 seconds until 1.3.0) |
| `SEARCH_PROVIDER_MAX_COOLDOWN_SECONDS` | `3600` | Cap for the doubling cooldown of a provider that comes back and is refused again |
| `SEARCH_PROVIDER_FAILURE_LIMIT` | `3` | Failures in a row, other than walls, that start a cooldown |
| `SEARCH_PROVIDER_WINDOW_SECONDS` | `1800` | How far back the recent calls that order the providers reach |
| `SEARCH_PRIMARY_BUDGET_MS` | `15000` | How long a deep search's primary-source angle may run while the side angles are cut at `SEARCH_ANGLE_BUDGET_MS` (`0`: the same) |
| `SEARCH_RESOLVE_DATES` | `auto` | `auto` (news and date-range searches), `on` (every search) or `off` (never, whatever a call says) |
| `SEARCH_RESOLVE_DATES_TOP` / `_MAX` | `6` / `12` | Results whose page head is read for a date (`_MAX` with `strict_dates`) |
| `SEARCH_RESOLVE_DATES_BUDGET_MS` | `4000` | Time the date reads may take together |
| `SEARCH_RESOLVE_DATES_BYTES` / `_HEAD_CAP` | `65536` / `262144` | The first bytes of a page read, and how far a head that has not ended is read on |
| `SEARCH_RESOLVE_DATES_CONCURRENCY` | `4` | Date reads at once (two of one host) |
| `SCRAPE_RECOVER` | `on` | `off` never reads a walled page another way, whatever a call says |
| `RECOVER_BUDGET_SECONDS` / `RECOVER_MAX_ALTERNATES` | `30` / `6` | Time the ways round a wall may take together, and the most `alternates` listed |
| `OFFICIAL_HOSTS` / `AGGREGATOR_HOSTS` | empty | Hosts (or `host/path`) added to the first-party and aggregator tables |
| `BRAVE_SEARCH_API_KEY`, `SERPER_API_KEY` (no prefix) | unset | Turn on the Brave Search API and Serper; never logged or answered |
| `SCRAPE_MAX_LINKS` | `50` | Links `web_scrape` lists by default, from the page content; a request's `max_links` (0 to 200) replaces it |
| `SCRAPE_BATCH_CONCURRENCY` | `4` | Pages one `web_scrape` call with `urls` reads at a time (and at most 2 from one host) |
| `SCRAPE_BATCH_MAX_TOTAL_CHARS` | `60000` | Cap on a whole batch answer, in characters; a request's `max_total_chars` (5,000 to 500,000) replaces it |
| `SEARCH_READ_MAX_TOTAL_CHARS` | `60000` | Cap on a whole `web_search` answer that read pages (`read`), the content included |
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
| `PAGE_CACHE_TTL_SECONDS` | `300` | How long a fetched page is kept for scrape and extract; `0` turns the page cache off |
| `PAGE_CACHE_MAX_ENTRIES` / `PAGE_CACHE_MAX_BYTES` | `64` / `20000000` | The page cache's bounds, in pages and in bytes of HTML |
| `BROWSER_ENABLED` | `true` | Enable the headless worker |
| `BROWSER_PREFERENCE` | `auto` | `auto`, `chrome`, `chromium`, or `camoufox` |
| `BROWSER_POST_LOAD_WAIT_MS` | `500` | The least a page is waited for after it has loaded (1.4.0: it was the whole wait) |
| `BROWSER_SETTLE_MAX_MS` | `8000` | The most a page that is still a shell is waited for, once the load is done (and never past `BROWSER_TIMEOUT_SECONDS` for the whole render) |
| `BROWSER_SETTLE_STABLE_MS` | `600` | How long the page's visible text must have stopped growing for the wait to end |
| `BROWSER_MAX_CONTEXTS` | `4` | Concurrent browser contexts |
| `BROWSER_RENDER_MIN_CHARS` | `200` | A page with fewer readable characters than this and scripts in its HTML is read again in the browser (1.4.0: it was 50 unless the HTML had `id="root"` or `id="app"`); a rendered page with under a quarter of it, or a loading placeholder, is a shell |
| `SOFT_404_CHECK` | `true` | `false` never asks a host for a path that cannot exist ([Pages that do not exist](#pages-that-do-not-exist-soft-404)) |
| `RESPECT_ROBOTS` | `true` | Crawl robots.txt policy |
| `MAX_CRAWL_PAGES` | `100` | Hard page cap |
| `MAX_CRAWL_DEPTH` | `4` | Hard depth cap |
| `CRAWL_CONCURRENCY` | `4` | Pages fetched in parallel by a crawl job |
| `MAX_CRAWL_JOBS` | `32` | Retained job cap, in memory and on disk |
| `DATA_DIR` | `~/.local/share/web-research` (`$XDG_DATA_HOME/web-research`) | Folder where crawl jobs are kept (`crawl-jobs.sqlite3`); empty or `off` keeps none |

`API_WEB_RATE_LIMIT_PER_MINUTE` controls authenticated REST access to the web
routes. Existing general and scrape research limits are unchanged.

## Dot Desktop integration

The package is published as `dotagent-web-research` (`pip install dotagent-web-research`,
`uv tool install dotagent-web-research`) and installs a `web-research-mcp` command that
starts this server over stdio. Dot's package depends on it, so a fresh Dot install brings
it, and its table is `command = "web-research-mcp"`. From a checkout (the owner's), the
launcher below does the same.

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
  before any request. A literal is judged by the address it reaches: an IPv6
  form that carries an IPv4 address (IPv4-compatible `::127.0.0.1`, IPv4-mapped
  `::ffff:127.0.0.1`, IPv4-translated `::ffff:0:7f00:1`, NAT64 `64:ff9b::7f00:1`,
  6to4 `2002:7f00:1::`) is judged as that IPv4 address, so one that carries a
  private, loopback or link-local address is refused and one that carries a
  public address (what a DNS64 gateway answers for an IPv4-only site on an
  IPv6-only network) is not; NAT64's local-use prefix `64:ff9b:1::/48` is
  refused whole. A host written as a number in an old form (`127.1`,
  `0x7f.0.0.1`, `0177.0.0.1`, `2130706433`) is refused: `0177.0.0.1` is
  127.0.0.1 to libcurl and may be 177.0.0.1 to `getaddrinfo`, so the address
  that was checked would not be the one reached. A real IPv4 address is four
  decimal numbers.
- **Every connection goes to an address that was checked in the same step**
  (1.3.1). A host name is resolved once, inside the connection; every address of
  the answer must be public (one that is not refuses the name, whatever else it
  answers); and the TCP connection is made to one of those addresses, IPv4
  first, never to a second lookup. The host name stays the request's own: TLS
  verifies the certificate for it, SNI included, and the `Host` header is the
  URL's. A redirect is a new request and goes through the same step, so a
  name that answers a public address to one connection and `127.0.0.1` to the
  next is connected to the public address once and refused the next time.
  Before 1.3.1 the check and the connection each looked the name up (a DNS
  rebinding answer, public to the first and private to the second, was checked
  as public and connected to as the loopback or the home network); this
  includes the reads the server makes of its own accord: the dates read from
  the heads of search results, and the recovery of a walled page (its AMP or
  print form, its feed, another outlet's copy). This holds for
  - `SafeFetcher` (`web_scraper/pinned_transport.py`): scrape, map, extract,
    crawl, `robots.txt`, sitemaps, the head reads of `resolve_dates`, and every
    recovery way but the archive. A proxy from the environment is never used
    (it would resolve the name itself);
  - the Internet Archive's curl session (`web.archive.org`, fixed in code): the
    transport resolves the name, checks it and gives libcurl those addresses
    as the only ones the name has (`CURLOPT_RESOLVE`,
    `web_scraper/curl_pins.py`), with no proxy from the environment, and an
    answer that came from a private address is not returned;
  - the older scrapers (`WebScraper` and `WebScraperAsync`: the CLI, the REST
    `/scrape` and the research agent) in the same way, with the URL normalized
    first (libcurl matches a pin by the host as written, and `example.com.`
    is not `example.com` to it). `SCRAPER_ALLOW_PRIVATE_NETWORKS=true` lifts
    the public-address rule, not the pinning.
- **What the pin does not cover.** Said plainly, so that the guarantee is not
  read wider than it is:
  - *The headless browser* (and the older Playwright scraper, `web-scraper
    --playwright` and the research agent's escalation to it, which has the
    same guard since 1.3.1; before, it checked only the address it was given
    and not what the page redirected to or loaded). A browser resolves names
    itself and cannot be told which address to use for one, so a page is
    rendered only if its host resolves to public addresses only at the moment
    the render is asked for, and every request the page makes is held to the
    same rule (`RequestGuard`: the verdict on a host is kept 30 seconds if it is
    a yes, 300 if it is a no). What the guard holds, since 1.4.2: it is put on
    the browser *context*, before its first page exists, so it holds the
    requests of the page, of its frames and of its dedicated workers, and of a
    window the page opens (a popup is a page of the context; 1.4.1 routed the
    one page only, and `window.open("http://127.0.0.1:8080/…")` and everything
    the window loaded reached the host); the **web sockets** of the page and its
    frames (a socket is not a request a route sees: `new
    WebSocket("ws://127.0.0.1:9222/…")` reached the host on 1.4.1; now a socket
    to an address that may be requested is connected as before and any other is
    closed with 1008, the page sees `close`); and a window is closed the moment
    it opens (nothing waits for it, and its settle wait does not keep it alive).
    A Playwright before 1.48 cannot route web sockets, and is not used (the
    render says `browser_unavailable`; the `playwright` extra asks for 1.48).
    What remains is the browser's own resolution: a
    name that answers a public address to that check and a private one to the
    browser's lookup is rendered; **and a web socket opened inside a Web
    Worker**, which Playwright does not route (it reaches the host the guard
    would have refused; the fix is a pinning proxy, not a longer list). A
    `web_map` of a site reads its start page through the same loader, so in the
    same browser under the same rule: the browser resolves the name itself,
    as it does for a scraped page. The server's headless browser runs only for a
    page that the pinned HTTP fetch showed to need rendering (little text and
    scripts), or that answered an access wall, and for the probe of the
    missing-page check of a host whose pages are drawn by scripts (its address is
    checked as any page's is, and so are the requests the page makes).
  - *The fixed hosts.* The search providers (DuckDuckGo, Yahoo, Google; Brave and
    Serper at `api.search.brave.com` and `google.serper.dev`) and the research
    agent's own collectors (Wikipedia, Hacker News, arXiv, Stack Exchange,
    PubMed, four news feeds) are named in code, not by a page or a result, and
    the keyed ones are `https` endpoints where a certificate for another address
    would not verify, so a rebinding answer cannot be made for them by a
    stranger. They are read through their own sessions and are not pinned.
    Google's result links are the one thing taken from a page there, and
    until 1.3.1 any link that held `/url?` was requested to find the real
    address (a blind request to wherever a result pointed, a local server
    included): now only Google's own redirect wrappers
    (`https://www.google.com/url?q=...`, `/goto?url=...`) are.
  - *FlareSolverr* (`FLARESOLVERR_URL`, the older scrapers' option for a
    Cloudflare challenge) and the *language model host* are services of the
    operator's own and are reached where the operator pointed them.
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
provider call would instead put that provider in cooldown, and the fallback chain
would answer while it rests. When the provider cooperates, `deep` adds its
fourth angle; when it does not, retry later or use `fast` to conserve provider
budget.

### Search relevance evaluation

`scripts/evaluate_web_search.py` scores a live `web_search` against
`benchmarks/search_relevance_queries.json`: ten labelled queries whose expected
hosts (or subdomains) mark a result relevant. It measures the whole ranked list,
not just the first hit: nDCG@5, MRR, official-source hits in the top 3,
freshness hits in the top 5, unique domains, provider warnings, and the returned
top results. The run writes `benchmarks/web_search_relevance.json`; pass
`--no-write` to inspect a run without replacing it, or `--output` to write it
elsewhere (the order of the rows of that file is pinned by
`tests/fixtures/ranking_of_recorded_queries.json`).

Two `discover` runs of 2026-10-22 (`limit=8`, DuckDuckGo) are recorded, and each
answered all ten queries:

| Metric | Search v2 alone | Search v2 with the reliability work |
|---|---:|---:|
| Mean nDCG@5 | 0.853 | 0.837 |
| Mean MRR | 0.833 | 0.850 |
| Official source in top 3 | 9/10 | 10/10 |
| Freshness hit in top 5 | 2/2 | 2/2 |
| Mean unique domains in top 5 | 4.4 | 4.1 |

The first run (15:12 UTC) is `benchmarks/web_search_relevance.json`; three of its
queries carry `background_query_failed`. The second (16:37 UTC) is
`benchmarks/web_search_relevance_integration.json`, made on the branch that
integrated search v2 with the reliability work; four of its queries carry
`provider_fallback`, the Google fallback having answered. Both are read by
`tests/test_web_search_floor.py`. Nine queries scored nDCG@5 of 0.62 or more in
each run. The exception of the second was `Türkiye yapay zeka eylem planı 2026`
(nDCG 0): the provider returned no page on the fixture's expected domain, so the
ranking had nothing relevant to place; the first run scored it 1.0. Labels are
expected domains, not human relevance grades, and a live run moves with provider
coverage; compare runs on the same fixture rather than treating one score as an
absolute quality claim. Neither run is of the ranking of version 1.2.0: run the
script again to measure it.

The ranking can also be measured offline on the recorded rows: re-rank the
recorded top five of each query (titles and addresses; a row holds no snippet)
and score it with the same labels. On 2026-10-23, with BM25 scaled to the pool's
best match, as it was until the integration branch was merged, the two runs came
to a mean nDCG@5 of 0.792 and 0.703 (MRR 0.745 and 0.650). With BM25 squashed
into (0, 1), the integration branch's way and the ranking now, they came to
0.928 and 0.815 (MRR 0.933 and 0.783): ten of the twenty rows higher, none
lower, an official page (`react.dev`'s `useEffect`, `sqlite.org`'s `fts5`)
coming before pages that spell the whole query. The integration branch also
weighted BM25 by `relevance`; with it the means were 0.869 and 0.822 (MRR 0.833
and 0.800), three rows lower than with the squash alone and one higher, and
that weighting is not in the ranking. These compare rankings of five results
each; they are no live measure.

## Verification

Checks actually run during this work:

```bash
.venv/bin/python -m pytest -q          # 952 passed
.venv/bin/python -m ruff check web_scraper tests scripts   # All checks passed
.venv/bin/python -m ruff format --check web_scraper tests scripts  # 118 files already formatted
.venv/bin/python scripts/benchmark_web_tools.py   # 6/6 checks passed
.venv/bin/python scripts/load_test_web_tools.py   # 10/10 checks passed
```

The live `fast` / `discover` / `deep` comparison above was also run against
DuckDuckGo on 2026-10-22. The full suite count includes the discovery, search
cache, cooldown, timeout, locale, and BM25F tests in `tests/test_web_discovery.py`
and `tests/test_web_service.py`, the pacing, fallback, and cooldown tests in
`tests/test_web_search_resilience.py`, the empty-answer tests in
`tests/test_web_search_honesty.py`, the floor under relevance, with the two
recorded runs, in `tests/test_web_search_floor.py`, and the source-type tests in
`tests/test_web_source_types.py`. The pytest, ruff and load-test lines were run
again after `main` (the integration branch, PR #5) was merged into this branch
on 2026-10-23; the benchmark line is the one of the earlier work.

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
  with a benchmark. Since 1.4.0 the browser waits for a page to settle and a page that
  stays a shell says so, but a page that needs a login, a click or a scroll to show
  its content is still a shell (`rendered_shell`).
- The wait for a page to settle watches the page's text, and cannot tell a page from what stands in front of it. A
  consent banner or any other text of 50 characters or more that shows up before the content, when the network has gone
  quiet and the text has not changed for `BROWSER_SETTLE_STABLE_MS`, is read as the page: the answer is that text and
  carries no `rendered_shell`. A page whose content comes later than the network goes quiet, from a timer or a scroll,
  is read as it is at that moment. `BROWSER_SETTLE_MAX_MS` and `BROWSER_SETTLE_STABLE_MS` are the knobs.
- The missing-page check judges the page it was given. A page that the site redirects to its home page (the final address
  is the root of the site, `url` differs from `requested_url`) is the home page and is not judged; and a page that names
  itself by a canonical link is taken at its word. A site whose fallback differs from address to address (its words
  change with the path by more than the address's own) is not found.
- `web_extract` does not use the scrape cache (it holds scrape answers) but shares the
  page cache with `web_scrape`: a page fetched in the last 5 minutes is not fetched again.
- The scrape, search and page caches live in one process and are lost on
  restart (crawl jobs are not: they are kept on disk, see "Crawl jobs outlive a
  restart"). REST and MCP have separate caches. Provider cooldown state is also
  per process.
- The default DuckDuckGo path can be rate-limited. A 202 bot-detection response
  is reported as `search_failed` with `detail=bot_detected`; a 200 response with
  no result nodes still returns an empty list, which is a provider quality
  signal. A wall (and, since 1.3.0, the third failure in a row of another kind)
  starts that provider's 10-minute cooldown; with a fallback registered, later calls go to the
  fallback chain while it rests. The
  adapter can be replaced without changing the service contract: one written as
  `search(query, limit)` is called so when no date filter or option is asked.
- `discover` and `deep` send 3 and 4 provider queries instead of 1. They cost
  more latency and provider budget than `fast`; use `fast` when that matters.
  An individual angle failure is surfaced in `warnings` and may reduce coverage.
- `recency` is inferred from a plausible year in the title, snippet, or URL. Since 1.3.0 a
  result's `date` can come from its page's own metadata (`resolve_dates`, on for news and
  date-range searches), but only for the top few results within a time budget, and the read
  stops at 256 KB of a head: the rest keep the date their address, snippet or title showed, with
  a `date_confidence` under 0.5. Results whose year is not visible are labelled `unknown` and
  receive no current/historical bonus.
- Source type is a host-and-path heuristic ([How each source type is decided](#how-each-source-type-is-decided)). A blog hosted on a first-party domain (a vendor's
  own `blog.`) is `official` by design, a site that is not in the tables (a regional outlet) is
  `other`, and the aggregator table is a list, not a classifier; the per-domain cap still prevents it from filling the
  page. A host named as the query's subject is `official` on the top-level domains organizations
  use (`com org io dev ai app co`): a copy of the site on one of those is taken for the original,
  and the original on another (`.so`, `.sh`) is not named official by this rule. The rule cannot tell the exact-match
  domain of a generic query from the subject's own site (`cookrice.com` is `official` for "how to cook rice"): it needs two
  words and six letters, and a site the rule gets wrong, or misses, is for `WEB_TOOLS_OFFICIAL_HOSTS`. Scores are relative to the current result set, not calibrated relevance.
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
- (Until 1.3.0; see "Providers that heal" for the rule now.) A failure cools down only the
  provider that failed: 5 s (`SEARCH_PROVIDER_COOLDOWN_SECONDS`), doubling while failures repeat up to
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
  `oamk.fi`, and `ouka.fi` (the City of Oulu). `yle.fi`, the public
  broadcaster, was in this list until round 3, which made it a `news` outlet.
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
  warnings stay empty (`tests/test_web_search_floor.py`); of the second recorded
  run (see "Search relevance evaluation") one result goes, Edge's release notes
  under `MDN AbortController`, which holds no word of the query in its title or
  address (the row records no snippet, which may hold it).

### Search that does not fall to the browser every time

- A refused DuckDuckGo search went to the headless Google, which takes 6 s to
  tens of seconds when cold (one `fast` call took 12.8 s, a `deep` one 6-7 s).
  The fallback is now a chain, `SEARCH_FALLBACK_PROVIDERS`, `duckduckgo_lite,google`
  at first and `duckduckgo_lite,yahoo,google` since "Yahoo before the browser" below: `duckduckgo_lite` is DuckDuckGo's lite page
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

### Search parameters

`web_search` takes six optional parameters beside `query`, `limit`, `provider`
and `depth`; with none of them a search is what it was, and the server's
`SEARCH_REGION` and `SEARCH_LANGUAGE` stay the defaults.

| Parameter | Value | Becomes |
|---|---|---|
| `region` | `fi-fi`, `us-en`, `wt-wt` (country-language) | DuckDuckGo `kl` (and `bing_market`); Google `gl`, the country part, none for `wt-wt` |
| `language` | `fi`, `en`, `pt-BR` | Google `hl`; DuckDuckGo takes its region from `kl` and gets the language as the call's `Accept-Language` (`fi,en;q=0.5`), which it may or may not honour |
| `sites` | up to 5 domains | `site:` operators on every angle (`(site:a.com OR site:b.com)`), and a post-filter keeping only results on those domains or their subdomains |
| `exclude_sites` | up to 5 domains | `-site:` operators, and a post-filter dropping those domains and their subdomains |
| `date_from`, `date_to` | ISO dates, `2026-10-01` | DuckDuckGo `df=2026-10-01..2026-10-20` (an open side is closed by today, or by 1990-01-01); Google `tbs=cdr:1,cd_min:10/1/2026,cd_max:10/20/2026` |

- A domain may be given as a URL (`https://docs.python.org/3/`), in capitals or
  with `www.`: it is reduced to `docs.python.org`. `notpython.org` and
  `python.org.evil.example` are not on `python.org`.
- A result with a known date outside the range is dropped; one with no date is
  kept. The date is the one a result shows (`date`, read from its address,
  snippet or title); a month (`2026-10`) is inside when any of its days is.
- Against an explicit range the year-based angles are not added: `discover`
  and `deep` send no `current` (the year) and no `background` (history) angle,
  and a freshness cue in the query ("latest") sets no relative filter; the range
  replaces it. Every angle carries the parameters, the operators take room from
  the query rather than from the provider's length limit, and the fallback
  providers get the same parameters.
- The search cache key holds all six (the order of the domains does not matter),
  so a search with another region, site or range is a new search.
- A bad value is refused: HTTP 422 on REST, `invalid_arguments` on MCP,
  `invalid_request` from the service (`region` such as `finland`, more than five
  domains, a `date_from` after `date_to`).

### Scrape output: links on request

- `web_scrape` takes `links` (true by default) and `max_links` (0 to 200, 50 by
  default, the server's `SCRAPE_MAX_LINKS`; held to `MAX_LINKS`, 500).
  `links: false` returns none, and `links_truncated` is then `false`: nothing
  was left out, the caller asked for none. `max_links: 0` with links on returns
  none and `links_truncated` says the page has some.
- Each limit bounds one thing: `max_chars` the Markdown only, `max_links` the
  links. A page's menus, headers, footers and sidebars are still left out of
  the list and still count towards `links_truncated`; `web_map` lists every link.
- The scrape cache key holds the choice, so a scrape without links and one with
  200 are separate entries.

### Reliable sources first

The deployed search was weighed on news queries (2026-10-23, 727ec4f): besides
WSJ and Democracy Now!, most results were small aggregator sites, a Facebook
video and unknown blogs, and they scored highest. An aggregator that copies a
headline wins on BM25 and freshness, and only 17 English-language outlets were
`news`; everything else, a post included, was `other` with authority 0, never
less.

- **Outlets by name** (`web_scraper/web_tools/sources.py`, one frozenset per
  group with a comment): wire services (Reuters, AP, AFP, UPI, dpa, EFE, ANSA,
  STT, Anadolu), national and public broadcasters (BBC, YLE, NHK, DW,
  France 24, TRT, NPR, PBS, CBC, ABC Australia, SVT, NRK, DR, ...), the papers of
  record and major outlets of the US, the UK and Ireland, the EU (Le Monde,
  Spiegel, El País, Corriere, ...), the Nordics (Helsingin Sanomat,
  Kauppalehti, Kaleva, Dagens Nyheter, ...), Turkey (Hürriyet, Sabah, Sözcü,
  Cumhuriyet, Habertürk, ...), Asia, the Americas, the Middle East and Africa,
  and the business and tech press (Bloomberg, FT, CNBC, TechCrunch, The Verge,
  Ars Technica, Wired, ...). A host matches itself and its subdomains. It is kept
  to established outlets: a site is not `news` because its name sounds like it
  (`newsnow-ai-daily.com` is `other`), and tabloids and state propaganda outlets
  are left out. `yle.fi` moved here from `official`.
- **`social`**: Facebook (`fb.watch` too), Instagram, TikTok, X and Twitter,
  Threads, LinkedIn, Bluesky, Telegram, and YouTube *for a news query only*: a
  tutorial on YouTube is what was asked for, a video about the news is not the
  story. **`press_release`**: PR Newswire, GlobeNewswire, Business Wire,
  ACCESS Newswire, EIN Presswire, PRWeb, Mynewsdesk, STT Info, and the like.
  Both have authority 0 on every other query, as `other` has.
- **Blog hosts** (`blogspot.com`, `wordpress.com`, `substack.com`, Tumblr,
  Ghost, ...) count as `community`.
- **Ordering on a news query** (a freshness cue, the current year, or the word
  "news" or its Finnish, Turkish, Swedish, German, French, Spanish and Italian
  forms): an outlet gets +0.05 on top of its authority, `community` loses 0.06,
  a `press_release` 0.24 and a `social` post 0.30. A relevant, fresh article of
  an outlet then goes above a page of an unknown site, a post or a press release
  of the same freshness, whatever its BM25. The date still decides when the
  query says how fresh: it weighs 0.30, so a fresh page goes before a stale
  outlet's article. Diversity holds: the per-domain cap and the source-type
  adjustment run after, on these scores.
- `depth: fast` keeps the provider's order, as it always did: the types are
  labelled on its results all the same, but only `discover` and `deep` rank by
  them.
- The ten recorded queries of `benchmarks/web_search_relevance.json` are not
  touched by the types (none of their results is an outlet, a post or a press
  release): `tests/test_web_search_sources.py` holds their order, as BM25
  squashed into (0, 1) puts them (see "Search relevance evaluation").

### Several pages in one call

- `web_scrape` takes `urls` (2 to 10 URLs) in place of `url`: exactly one of the
  two. The answer is `{"pages": [...], "timings": {"total_ms": ...}}`, the pages
  in the order given. Each is that page's own scrape answer (with
  `markdown_truncated`, below) or `{"url", "error"}` with the error envelope (code,
  message, url, status_code, detail and, for a walled page, its `hint`). One
  page failing never fails the others; `links`, `max_links`, `max_chars` and
  `archive` apply to every page. The single-URL answer is unchanged.
- Limits: `SCRAPE_BATCH_CONCURRENCY` pages at a time (4 by default) and at most
  two at a time from one host, whatever the number of hosts: a page waits for its
  host's turn before it takes one of the four, so a queue on one host never keeps
  another host's page waiting. The fetcher's own per-host gate and the page cache
  still apply, so a page read before is served from the cache.
- The whole answer stays under `max_total_chars` (60,000 characters of compact JSON
  by default, 5,000 to 500,000). The pages whose Markdown is over one shared
  length are cut to it, so a long page loses most and a short one nothing; a cut
  Markdown ends with `…` and its page says `markdown_truncated: true` (it is
  `false` on every other page of a batch, and absent from a single answer). Only
  if that cannot fit do the links, then the metadata, then the rest of the
  Markdown give way (`links_truncated` and `metadata_truncated` say so). A
  cut never reaches the cache: a single scrape of the same URL afterwards is whole.
- REST: `POST /api/v1/web/scrape` answers a batch with the same shape; a request with
  both `url` and `urls`, or neither, is refused (HTTP 422). The schema keeps `url` a
  string and `urls` an array (2 to 10) with no top-level `oneOf`.
- The REST 422 body no longer turns into a 500 when a validator raised
  `ValueError` (the exception object was left in `ctx.error`): `details[].ctx.error` is
  now its message.

### Search that can read its top results

- Search gave links and snippets, not the articles, so an agent searched and
  then scraped each result by hand. `web_search` takes `read` (0 to 5, default
  0): the top N results, in the order the answer has them, are read in the same
  call with the batch scrape of "Several pages in one call".
- Each result read carries `content`, its Markdown cut to `read_chars` (4,000 by
  default, 500 to 20,000) with `content_truncated` (`true` when the page has more),
  or `content_error`, the error envelope (code, message, url, status_code, detail
  and, for a walled page, its `hint`). The results that were not read carry
  neither, and with `read: 0`, the default, the answer is exactly what it was
  (no `content` field, no `read_ms`).
- Reading costs seconds: `timings.read_ms` says how many, and `total_ms` includes
  it. The pages are read together (4 at a time, 2 per host) through the scrape
  cache, so a page read a minute ago is not fetched again.
- The whole answer stays under `SEARCH_READ_MAX_TOTAL_CHARS` (60,000 characters of
  compact JSON): the contents over one shared length are cut to it, each ending in
  `…` with `content_truncated: true`, so a long page loses most and a short one
  nothing.
- A walled page is not retried from the Internet Archive unless asked: the request
  takes `archive` as `web_scrape` does, and a walled page's `content_error` carries
  the hint to ask for it.
- The search itself is cached as before, without the reading (the key does not hold
  `read`): a search repeated with another `read` asks the provider once and each
  page is read once; a search with `read: 0` never gets the content another asked
  for.

### Extract: paging past the cap

- A field with `all: true` returns at most `max_items` values (100 by default, 1 to
  1,000) per field; the request schema carries it under
  `$defs.WebExtractField.properties` and the tool description names it, for a client
  that does not follow `$ref`. What was missing was a way to read the rest.
- `offset` (0 to 1,000,000), per field, with `all: true`, skips the first N matches
  (the non-empty values, in document order); `max_items` is the page's size. The
  field's provenance holds `offset` (0 when none was asked) and, when matches are
  left after the page, `truncated: true` and `next_offset`, the offset to ask for
  next. The last page has no `next_offset` and `truncated: false`. An offset past
  the last match answers an empty list for a field that exists, not a missing field.
  A field that is not `all` ignores it.
- The character cap (`max_chars`) still bounds each answer: when it cuts values from
  the end of a page, `returned` and `next_offset` follow what was kept, so paging on
  from `next_offset` loses nothing. A list of 250 items is read in three pages:
  `offset` 0, 100, 200 returned 100, 100 and 50, the first two with `next_offset`
  100 and 200 (`tests/test_web_extract_paging.py`). Fields read by their name
  (`keywords`, a JSON-LD list) page the same way.

### Crawl seeded from the sitemap

- A crawl found a site's pages by their links, so the pages no link reaches (an
  archive, an orphaned article) were missed. `use_sitemap: true` in the crawl's
  options (`web_crawl_start`, `POST /api/v1/web/crawl`; false by default) seeds the
  frontier from the site's sitemaps, read as `web_map` reads them: the `Sitemap:`
  lines of robots.txt, `/sitemap.xml`, and the sitemaps an index names (at most
  `MAX_SITEMAPS`).
- The seeds come right after the start page, in the sitemap's order, as its
  equals (depth 0, so a seed's own links are followed to `max_depth` as ever).
  They are kept in scope (the host, and its subdomains only with
  `include_subdomains`), through `include_patterns` and `exclude_patterns`, and
  behind robots.txt (a disallowed seed is counted in `skipped`), and they count
  against `max_pages`: no more than `max_pages - 1` are queued, so a crawl of 20
  pages over a 500-page sitemap reads the start page and the first 19 seeds, and
  link-following has what is left, none here. A page both listed and linked is read
  once.
- The job's `progress.seeded` says how many seeds were queued and its `options`
  show `use_sitemap`. A sitemap that cannot be read is an entry in the job's `errors`,
  not its end, and a site with no robots.txt is not an error of it.
- Covered by `tests/test_web_crawl_sitemap.py`: a fake site whose sitemap lists pages
  no link reaches, scope, filters, robots, `max_pages`, an index, and a missing sitemap.

### One fetch for scrape and extract of the same page

- The scrape cache holds the scrape's *answer*, keyed by URL, `max_chars`, links and
  `archive`; `web_extract` used none, so scraping a page and extracting from it
  fetched it twice (and so did two scrapes with different caps). A short-lived page
  cache now sits at the fetch layer (`web_scraper/web_tools/pagecache.py`): it holds
  the fetched document, the HTML as the tools last read it (rendered by the browser
  when the page needed it), keyed by the page's canonical URL, and each call parses
  it as it needs, with its own `max_chars`, links and selectors.
- It lives `PAGE_CACHE_TTL_SECONDS` (300; 0 turns it off) and is bounded in documents
  (`PAGE_CACHE_MAX_ENTRIES`, 64) and in bytes of HTML (`PAGE_CACHE_MAX_BYTES`, 20 MB):
  the oldest unused goes first, and a page that alone is over the byte bound is not
  kept. `GET /api/v1/web/stats` lists its entries, bytes, hits and misses.
- Never kept: an error of any kind; a challenge page (a captcha, a login wall, an
  `access_blocked` page are errors, so the next call tries again); an archived copy
  (`archive: true`); a PDF; a page with nothing to read (`empty_content`) or whose
  browser render failed; a page read by a crawl.
- `web_extract`'s answer says `cache_hit`, `true` when the page came from the page
  cache. Its timings do not pretend to fetch: `network_ms`, `http_ms` and `browser_ms` are
  0, `extract_ms` and `total_ms` are this call's, and `cached_timings` holds what the
  fetch cost when it was made. The same holds for a scrape's `timings` when the page was
  stored (its `cache_hit` still means the scrape *answer* came from the scrape cache).

### A cut angle says so

- Once the base angle has answered, the angles after it have `SEARCH_ANGLE_BUDGET_MS`
  (5,000 by default, 0 to 120,000, `0` waits for every angle; the base is never cut).
  An angle still out then is cut. Its warning used to be `<angle>_query_failed`, the
  same as a refusal, and only `queries[].error.detail == "angle_budget"` told them
  apart. It is now `<angle>_query_cut` (`current_query_cut`, `background_query_cut`,
  `primary_query_cut`); `<angle>_query_failed` is left to what the provider refused or
  failed.
- What a cut costs: that angle's results are missing from the ranking (the current-year
  results, the history, the official documentation), so the answer is narrower than
  a full `discover` or `deep`; the call that was in flight is dropped, and the narrower
  answer is cached for `SEARCH_CACHE_TTL_SECONDS` like any other, so the same search
  within five minutes is the same answer. A cut is the search's own limit: it starts no
  cooldown for the provider and no fallback asks for it. Raise the budget (or set it to 0)
  to wait for every angle, or use `depth: fast` for one query.

### Crawl jobs outlive a restart

- Jobs lived only in the manager's memory, and a restart of Dot's daemon restarts this
  server, so every job (its pages included) was lost. Each job is now kept in SQLite
  (the standard library) in a data folder, as it goes: its state, options, progress
  counters, errors, duplicates and results, the results appended as their batch of
  pages ends. The folder is `WEB_TOOLS_DATA_DIR`, by default `web-research` in the XDG
  data home (`$XDG_DATA_HOME`, else `~/.local/share/web-research`); the package has no
  data folder of its own (its research history takes a file path, relative to where the
  server runs). An empty value, `none` or `off` keeps nothing on disk. A folder this
  makes is `0700` and the database `0600`: crawled pages are the user's own.
- After a restart a job that was `queued` or `running` is **`interrupted`**
  (`finished_at` is when the server read it again), and so is one the server ended by
  stopping (a graceful stop of the daemon is not a `cancelled`). Its pages stay readable
  through `web_crawl_status` and `web_crawl_results`, as far as the last batch got; a
  job being cancelled becomes `cancelled`, and the finished ones read as they were.
  `interrupted` is final like `completed`: **resuming an interrupted job is left out**
  (start the crawl again; the page cache and the scrape cache make the pages already read
  cheap only while they live, which a restart ends).
- The bounds hold on disk: `MAX_CRAWL_JOBS` and `CRAWL_JOB_TTL_SECONDS` prune a job's
  rows and results together, as they prune it in memory, on every access and when the
  server starts, counted from a job's last update (an interrupted job from the restart
  that interrupted it). A job that is still going is never dropped to make room.
- A folder that cannot be made or written, or a database that is damaged, costs the
  persistence and not the crawl: the server logs one warning ("Crawl jobs are no longer
  kept on disk") and goes on in memory. The tests keep nothing in the owner's data
  folder (`WEB_TOOLS_DATA_DIR` is empty under them, and a settings made by hand names
  no folder). Covered by `tests/test_web_crawl_persistence.py`: a manager closed and
  made again over the same folder, a running and a queued job, a graceful stop, pruning
  on disk, the folder and a damaged database.

### What round 3 measured live (2026-10-23, this Mac, one run each)

About 30 calls in all, one search at a time, 8 s or more apart.

- **Search latency, same queries, base code (727ec4f) against this branch**, DuckDuckGo
  answering every call at that moment:

  | Query | depth | before | after |
  |---|---|---:|---:|
  | `python asyncio tutorial` | `fast` | 1.09 s | 0.69 s |
  | `rust borrow checker explained` | `fast` | 1.08 s | 0.72 s |
  | `AI regulation news this week` | `fast` | 0.93 s | 0.75 s |
  | `python asyncio tutorial` | `discover` | 3.90 s | 4.49 s |

  A `fast` search is one request instead of two (no token fetched from
  `duckduckgo.com`), a third quicker, with the same results. `discover` sends its three
  angles one at a time through the provider's gate, with its 250-500 ms jitter, as
  before: the difference is DuckDuckGo's and the jitter's, one run each.
- **The chain.** With the html endpoint made to refuse in the process (a simulated 202),
  DuckDuckGo lite answered a `fast` search in 0.81 s (`tried`: html failed
  `bot_detected`, lite answered); before, that search went to the headless Google. Later
  in the same session, after about 30 calls in 35 minutes from this address, DuckDuckGo
  refused *both* endpoints (`bot_detected`, `tried` shows each): the angles went to Google,
  and the base angle took 25 s while the headless browser started. Lite helps when one
  endpoint refuses (they judge a request by its form, and each has its own pace); it
  cannot when the address itself is flagged.
- **Archive.** Reuters' AI section answered 401/captcha; the CDX lookup answered 200
  with its copies (the newest of 2025-09-25); the replay of that newest copy answered the
  fetcher HTTP 429 whatever its headers (the fetcher's, only a user agent, or a user agent
  and `Accept: */*`), while `curl` got 200 for the same address a few seconds before and
  after. The 429 is the archive's reply to this client (`httpx`, HTTP/1.1), not a
  header the fetcher could change, so the walk ends with its hint (`The Internet Archive
  answered HTTP 429 ... try archive: true again later`) and the skip to a good copy could not be
  shown live from this Mac (the tests cover it with fake transports and the real
  interstitial check). Reading replays through `curl_cffi` was the way round it, done next:
  see "Wayback replays are read like a browser" below.
- **News search with `read`.** `AI regulation news this week` at `discover` with
  `read: 2`: Reuters (`news`) first; its content was a `content_error` (`blocked`, captcha,
  with the archive hint), `news.un.org` came back as 1,500 characters
  (`content_truncated: true`), `timings.read_ms` 2.1 s, the whole answer 7.4 KB.

### Wayback replays are read like a browser

- The live check above found `web.archive.org` answering the `httpx` client HTTP 429 on a
  replay whatever the headers, while curl got 200, so `archive: true` read no copy in practice
  (the CDX list answered; the replay did not). The CDX list and the replays of
  `web.archive.org`, and of no other host, are now read through a `curl_cffi` session that
  impersonates one Chrome (the newest the installed `curl_cffi` knows), as the DuckDuckGo
  searcher does (`web_scraper/web_tools/archive_fetcher.py`).
- It is the same `SafeFetcher` with another transport, so every limit is the fetcher's own: the
  byte cap (`MAX_RESPONSE_BYTES`, the download is stopped and closed when it is passed), the
  timeout (`FETCH_TIMEOUT_SECONDS`), the retry of a transient failure (`FETCH_RETRIES`), the
  redirect count, and the checks for a captcha, a login wall and a refusal, so an archived
  captcha is still skipped into `archive_skipped`. A redirect is followed only within
  `web.archive.org`; one out of it is refused like any other target.
- The host is fixed in code: only `https://web.archive.org` on its own port, with no
  credentials, is read this way (`web.archive.org.evil.example` and `archive.org` are not it),
  and the transport checks the host's addresses and pins them in the session (1.3.1: see
  [Security boundaries](#security-boundaries)). Nothing else goes through the
  impersonated session. The page itself is still fetched by the ordinary fetcher.
- Headers: the impersonated browser sends its own, coherent with its TLS fingerprint; the
  fetcher's user agent and `Accept` headers are not sent to the archive. The body arrives
  decoded, and a `Content-Encoding` that described the wire is left out of the answer so it
  is not decoded twice.
- Measured live (2026-10-23, three calls): Reuters' AI section answered 401/captcha and was
  read from the archive, copy of 2025-09-25, 2,324 characters (the CDX list in 1.0 s, the
  replay of 537 KB in 1.4 s, where `httpx` was answered 429); the New York Times'
  technology section answered 403 and was read from its copy of 2026-08-28 (11,211
  characters, the replay of 957 KB in 2.6 s); WSJ's AI section answered 200 and needed no
  archive. `archive_skipped` was empty in both reads: the newest copy the CDX lists with
  status 200 was itself readable. `tests/test_web_archive_transport.py` holds the limits with
  a fake session.

### Yahoo before the headless browser

- After about 30 calls in 35 minutes from one address, DuckDuckGo refused both its endpoints
  (html and lite) and the base angle fell to the headless Google, 25 s when cold. A second plain-HTTP
  provider, `yahoo`, now stands between `duckduckgo_lite` and `google` in the default chain
  (`SEARCH_FALLBACK_PROVIDERS=duckduckgo_lite,yahoo,google`), with its own gate and cooldown like
  every provider, and `provider: "yahoo"` asks it directly.
- Two candidates were probed once each, a minute apart, through an impersonating `curl_cffi`
  session (the stack an adapter uses), on `python asyncio tutorial`. **Yahoo**
  (`https://search.yahoo.com/search?p=...`) answered HTTP 200 in about a second with seven organic
  results in `div.algo-sr`. **Mojeek** (`https://www.mojeek.com/search?q=...`) answered HTTP 403,
  "Sorry your network appears to be sending automated queries so we can't process your search at
  this time"; nothing is added for it.
- The parser (`web_scraper/yahoo_search.py`, a fixture of the live answer in
  `tests/fixtures/yahoo_python_asyncio_tutorial.html`, scripts and styles stripped) reads each
  `div.algo-sr`: the title of its `h3.title`, the snippet of its `div.compText p` (Yahoo puts the date
  in front, `Jul 30, 2025 ·` or `2 days ago ·`, which the search's own date reading uses), and the
  address out of Yahoo's redirect (`.../RU=<encoded address>/RK=...`). A page's `Next` link is followed
  to at most three pages while fewer results than asked for are in. A result on Yahoo itself is never
  one.
- What it does and does not take: the relative date filter (`day`, `week`, `month` as Yahoo's
  `btf`; Yahoo has none for a year) and a call's `language` (as `Accept-Language`). A region and a
  date range are not sent (the date range still drops results dated outside it, after the search).
- Refusals: HTTP 403, 429 and 999, a redirect off `https://search.yahoo.com` (its consent page
  included), more than four redirects, and a page with no results that talks of unusual traffic or a
  captcha are `search_failed` with `detail: bot_detected`, as for DuckDuckGo. A redirect that stays on
  `search.yahoo.com` is followed by the searcher itself (Yahoo sends a first request through a
  `/_bv/v.gif` cookie step and back), never by the HTTP client, so nothing outside Yahoo is asked.
- Measured live (2026-10-23): `provider: "yahoo"` at `fast` answered in 2.4 s (the first request of the
  process, with that cookie step); with both DuckDuckGo endpoints made to refuse in the process, the
  chain's `tried` was `duckduckgo` failed, `duckduckgo_lite` failed, `yahoo` answered, in 0.6 s, where
  the headless browser had taken 25 s.


## Round A: dates you can trust, providers that heal, walled pages recovered, compact output (1.3.0, 2026-10-24)

A researcher used the server for a real job, the AI news of 16 to 23 October in 13 searches,
and rated it 7/10 (6.5 for production). Their evidence: DuckDuckGo gave way to Yahoo or
Google in 10 of the 13 searches and Yahoo once raised a `RuntimeError`; two `deep` searches had
their primary angle cut at 5 s; Reuters pages came back as a captcha and `archive: true` had no
copy; a `cloud.google.com` page with a published time came back with `date: null` and "Claude
Opus 5.5" mixed 22 September and 21 October; `openai.com` and `cloud.google.com` were typed
`other` and aggregators ranked above official sources; the search JSON swelled with facets,
queries and timings, and `web_scrape` wrote a long page as a one-line 20k or 44k file. What they
praised stays: `web_extract`'s JSON-LD provenance, `web_map`'s `lastmod`, and the transparency
of `warnings`, `timings`, `fallback`, `provider` and `provenance`. Compact output moves those
behind a flag and takes none away.

### Dates you can trust

`web_tools/dates.py` reads every date signal of a page as a `DateSignal` with the trust it
deserves alone, and weighs them together.

| Signal | `date_source` | Alone |
|---|---|---:|
| JSON-LD `datePublished` of the page's article (or WebPage) item | `json_ld:datePublished` | 0.80 |
| a second JSON-LD item's `datePublished` | `json_ld:<Type>.datePublished` | 0.65 |
| JSON-LD `dateCreated`, `uploadDate` | `json_ld:dateCreated` | 0.55 |
| `article:published_time`, `og:published_time`, `itemprop=datePublished` in a meta tag | `meta:article:published_time` | 0.75 |
| `itemprop=datePublished` on a visible element | `time[itemprop=datePublished]` | 0.70 |
| meta `parsely-pub-date`, `published_time` | `meta:published_time` | 0.65 |
| meta `sailthru.date`, `publish-date`, `publishdate`, `pubdate`, `article.published`, `citation_publication_date`, `dc.date.issued` | `meta:pubdate` | 0.60 |
| meta `dc.date`, `dcterms.date`, `dcterms.created`, `citation_date` | `meta:dc.date` | 0.55 |
| meta `date` | `meta:date` | 0.50 |
| the first `<time datetime>` of the page's own content (not a menu, a sidebar, a footer, a list of other stories) | `time[datetime]` | 0.50, 0.55 when marked as the publication time |
| a date printed near the headline (not inside the story) | `visible` | 0.35, 0.40 when labelled "Published" |
| the address, a day or a month | `url` | 0.35, 0.20 |
| the snippet: a day, "3 days ago" or "yesterday", a month | `snippet` | 0.35, 0.30, 0.20 |
| the title: a day, a month | `title` | 0.25, 0.20 |
| a date the provider gave as a field (Brave `page_age`, Serper `date`) | `provider` | 0.45 |

`dateModified`, `article:modified_time`, `og:updated_time` and the like are *modified* signals:
they give `date_modified` and are never the published date, so a story published on 22
September and updated on 21 October disagrees with nothing.

- **Agree.** Signals within a day of each other agree (a time zone is no conflict); a month agrees
  with a day inside it. A group of agreeing signals weighs `1 - (1 - a)(1 - b)...`: JSON-LD
  (0.80) and `article:published_time` (0.75) together are 0.95.
- **Date and confidence.** The heaviest group is the `date` (of equal weights, the earlier one:
  an update is stamped later than the story it updates). `date_confidence` is its weight times
  `1 - 0.5 w` for every other group of weight `w`: two strong signals that disagree leave about
  0.5. Under 0.5 a date is *low*; a date only the address or only the printed text gives always is.
- **Conflicts.** The signals outside the winning group are `date_conflicts`, at most five, each
  `{source, value}`; the "Claude Opus 5.5" page says
  `date: 2026-09-22, date_confidence: 0.5, date_conflicts: [{source: meta:article:published_time, value: 2026-10-21T09:00:00+00:00}]`.
- **Where it is said.** `web_scrape` and `web_extract` answer `date`, `date_source`,
  `date_confidence`, `date_conflicts` (and `date_modified` when the page has one), with no date
  said as `null` and a confidence of 0, never left out. In `web_extract` a named `published_at`
  or `modified_at` is held to the signals: found by its name first in JSON-LD, it is replaced by
  the winning signal when two others weigh against it, so `date` and `data.published_at` are never
  two different days; its provenance gains `date_confidence` and `date_conflicts`. Every search
  result carries `date_confidence` and `date_conflicts` over its own weak signals (address,
  snippet, title, provider); its `date` and `date_source` are what they were.
- **`resolve_dates`.** A result whose date is unknown or low (under 0.5) has its page's head
  read, for the top 6 results (`..._RESOLVE_DATES_TOP`; 12 for `strict_dates`), at most 4 at once
  and 2 of one host, within 4 s (`..._BUDGET_MS`, a read still out is cut and the answer says
  `resolve_dates_cut:N`), from the page cache when it holds the page. It reads the first 64 KB
  and, when the head has not ended by then, on to `</head>` up to 256 KB (`..._HEAD_CAP`): the
  live probe found `cloud.google.com`'s heads 170 KB long with the JSON-LD at byte 140,000, and
  64 KB dated none of them. A front page, a section or tag page, a PDF is not read (its date is
  other stories'). What is read is weighed with the signals the result showed, the results are
  ranked again with the dates found (freshness moves the order), and the answer's `dates` block
  says asked, read, from the page cache, resolved, failed, cut. On by default for a query that
  asks for news (the freshness cues, `news`, `uutiset`, `haber`...) or a call with a date range,
  and for `strict_dates`; `resolve_dates: true` or `false` decides, `..._RESOLVE_DATES=on` makes
  it the default for every search and **`off` makes it never happen, whatever a call says**.
- **`strict_dates`.** Ranks twice the limit, dates them, drops the results with no date or a low
  `date_confidence`, keeps the limit's worth of the rest and says `strict_dates_dropped:N` in
  `warnings`; an empty answer from the filter is not `no_relevant_results`.
- **Tests.** `tests/test_web_dates.py` has a head for each signal, conflicts, agreement,
  time zones, placeholders and future dates, the recorded head of a Google Cloud post
  (`tests/fixtures/cloud_google_trillium_head.html`, its scripts put back so that every date
  signal is past byte 120,000) and extract's settling; `tests/test_web_search_resolve_dates.py`
  the reads, the budget, the cache, the head read to its end and `strict_dates`.

### Providers that heal

- **A record per provider**, in the process: calls, answers, walls (`bot_detected`, a captcha),
  HTTP errors (`http_503`, `rate_limited`, `invalid_key`), timeouts, other exceptions (a
  `RuntimeError`, a parser given the wrong page), the latency of each call and the last error.
  An exception a provider raises is its failure and never the search's: Yahoo's HTTP error status
  is `http_<status>`, a refusing status (403, 429, 999) is `bot_detected`, a timeout is
  `search_timeout`.
- **Cooldown.** A wall starts one at once; any other failure starts one on the third in a row
  (`..._PROVIDER_FAILURE_LIMIT`). It lasts 10 minutes (`..._PROVIDER_COOLDOWN_SECONDS`), twice as
  long for each time a provider that came back is refused again, up to an hour
  (`..._MAX_COOLDOWN_SECONDS`); a provider that cools is skipped in the chain (`tried` says
  `skipped`, `provider_cooldown`) and asked only as a last resort. When the cooldown is over the
  provider is on probation: its record of failures is cleared and the next call is the probe, an
  answer clears the rest. A cut angle (the search's own budget) is no failure.
- **Healthy first.** The providers that are not cooling are ordered by their recent calls (the
  last 20 within 30 minutes, and only a record of 4 calls or more counts): 4 of 5 answered is
  good, half shaky, fewer poor; the configured order breaks ties and a cooling provider keeps
  its place. The provider asked first gives way to one in better standing
  (`reason: provider_unhealthy`).
- **Brave Search API and Serper.** `BRAVE_SEARCH_API_KEY` and `SERPER_API_KEY` turn them on;
  without a key a provider does not exist (a call naming it is told the variable, `web_health`
  lists it as `no_key`). A call that names no provider asks Brave, then Serper, then DuckDuckGo
  and the rest of the chain; a call that names one asks it first and the keyed providers last.
  The key is read from the environment, held in a closure, sent in a request header and
  nowhere else: not in a URL, a log line (the module has no logging), an error, a `repr`,
  `web_health` or an answer (`tests/test_web_paid_search.py` serialises all of them against a
  server that echoes the request). Brave is paced at one call a second (its free plan); their dates
  are a signal of a result. Note that every angle is a call: `depth: fast` costs one.
- **`web_health`.** `{providers: [{name, state, calls, ok, failures, block_rate, mean_latency_ms,
  last_error: {kind, code, detail, status_code, seconds_ago}, cooldown_remaining_s}], order,
  calls, cooldown_seconds, failures_before_cooldown}`; `state` is `ok`, `degraded`, `cooling` or
  `no_key` (with `key_env`). It reads memory and makes no request; 200 calls take a few
  milliseconds. `GET /api/v1/web/health` is the same.
- **A deep search's primary angle** is asked right after the base, before the side angles, so it
  queues first at a provider's gate, and may run to 15 s (`..._PRIMARY_BUDGET_MS`) while the side
  angles are cut at 5 s; `primary_query_cut` or `current_query_cut` says which was cut, and the
  angle's error says at what budget.

### Walled pages recovered

- **What is a wall:** a captcha, a bot or login wall (`blocked`), or 401, 403, 429. Any other
  failure keeps its hint (`archive: true`). `recover` defaults to on for a wall, unless `archive:
  true` is the only thing asked for (then, as before, only the archive); `recover: true` is every
  way whatever else is asked, `recover: false` none.
- **The ways,** together within 30 s (`..._RECOVER_BUDGET_SECONDS`): the Internet Archive's
  copy; the page's AMP and print forms (the wall's own `<link rel="amphtml">`, then `/amp`,
  `?outputType=amp`, `?print=1`, `?amp=1`: four at most, the site's own addresses only); the
  entry of the page in the site's RSS or Atom feed (the head's links, then `/feed`, `/rss.xml`...,
  the same site only), in full when the entry carries the article. The text of the page's own forms
  wins: the freshest form (AMP, print, then the archive) that holds at least 80% of the longest,
  the archive first when `archive: true` was asked for as well. When none gives the page, the
  title (what the wall still shows, else what the address spells) is searched away from the site
  and the first two results of an outlet or an official page that hold the story's words are
  read; posts, forums, repositories and aggregators are listed, never read as a report. A
  feed that carries only a summary is given only when nothing else reads.
- **Whose it is.** The answer never says it got the page when it got another source.
  `recovered_from` is `{kind: archive|amp|print|feed|other_source, url, ...}` (`at` for an
  archive copy, `partial` for a feed, `domain` and `title` for another outlet); the text begins
  with a Markdown line that says it (`> Recovered from the Internet Archive's copy of ... Not the
  live page`, `> Not the requested page. ... this is "..." from hindustantimes.com`), so a page
  written to a file carries it; for another outlet `url` is that outlet's and `requested_url` the
  walled page; `canonical_url` and `date` are those of the text read.
- **`alternates`** are the other addresses (`url, domain, title, date, source_type, why`: "78%
  of this story's title words, on hindustantimes.com (read)") and `next_action` one sentence to act
  on ("The text is hindustantimes.com's report, not reuters.com's: cross-check it with
  https://money.usnews.com/... or scrape ... again later").
- **When nothing reads,** the wall's error is raised, in MCP and REST both, with `hint` (what was
  tried: "archive: no archived copy; amp/print: none of the usual forms is served; ..."),
  `alternates`, `next_action` and `recovery_tried`.
- **What it does not do:** `web_search` with `read` hunts for no walled page (it was quick and
  stays so; `archive` still applies); AMP and feed addresses come from the walled page's own site
  only; `WEB_TOOLS_SCRAPE_RECOVER=off` turns it off for every call. A recovered answer is cached
  like any other.
- **Tests.** `tests/test_web_scrape_recover.py`: each way with fakes (a wall's own head, AMP and
  print addresses, an RSS and an Atom feed, the archive's CDX, another outlet found by search),
  the labels, the error's shape, the budget, the settings, and the recorded answer Reuters gave a
  plain client (`tests/fixtures/reuters_datadome_wall.html`: HTTP 401, 774 bytes, title
  "reuters.com", its tokens redacted).

### Compact output

- **`web_search`.** Per result: `title, url, domain, date, date_confidence, source_type, snippet,
  score` (and `content`, `content_truncated`, `content_error` where `read` asked for them), and
  `run: {provider, warnings, fallback}` (`fallback` is `{from, to, reason, tried}` of the first
  angle that took one; `cache_hit: true` when it did). A result whose dates conflict is named in
  `run.warnings` as `date_conflict:2`, so the eight fields stay eight. `debug: true` adds a
  `debug` block (`requested_provider, depth, facets, queries, timings, dates, cached_*`) and, under
  each result, the date's source and conflicts and the parts of the score; `compact: false` is the
  whole answer as it was.
  **Decision:** over MCP `compact` is on unless the call says `compact: false`, and the tool
  description says so plainly (the model reads it, and pays for every character); over REST the
  default stays the full answer, which the web UI and the benchmarks read. The search cache holds
  the whole answer, so both shapes share it. On the live query below the answer went from 5.5 KB
  to 2.8 KB.
- **`web_scrape lean`** returns `url, title, date, date_confidence, markdown, truncated,
  total_chars` and, when they are the case, the date's conflicts, `fetched_with`, everything of a
  recovery, the links that were asked for, and warnings; no metadata, timings, hash or links. A
  page cut by `max_chars` says `truncated: true`, `total_chars`, `next_offset` and, for a page the
  page cache keeps, a `content_id` (`c-` and 12 hex digits, the name of this text of this page).
  `web_scrape {content_id, offset, max_chars}` returns the Markdown from `offset` on, from the
  page cache and with no request, lean unless `lean: false`; it is the text the first read cut,
  part by part. An id the cache let go of (the page cache keeps a page 5 minutes) is a 410
  `content_expired` that names the page to scrape again; an answer cached longer than its page is
  read again, never repeated with a dead id. A PDF, a recovered page and a page when the page
  cache is off have no `content_id`.
- **The page in a block of its own.** Dot keeps an answer longer than a result holds in a file,
  and `web_scrape`'s was one line of 12k to 44k characters, the page's newlines escaped inside a
  JSON string (the saved `web_scrape` answers had no newline at all). Over MCP the answer
  is now the JSON on its first line, with `"markdown":"(below)"`, and the page's Markdown in a
  second text block: the file is the JSON line and then the page as Markdown with its own line
  breaks. A batch (`urls`) marks each page's block with `<!-- page 2 of 3: URL -->`; errors stay
  one block of JSON. REST is unchanged.
- **Tool text.** The schemas a model reads no longer say that an optional argument may be `null`,
  and the descriptions are shorter: nine tools in 11,504 characters (eight were 11,451).

### Source types that are right

- **First-party sites** are `official` (`sources.py`, `FIRST_PARTY_HOSTS`): `openai.com`,
  `anthropic.com`, `cloud.google.com`, `blog.google`, `deepmind.google`, `research.google`,
  `ai.meta.com`, `about.fb.com`, `microsoft.com` (its blogs and news included), the newsroom of
  `apple.com` (a host and a path), `aws.amazon.com`, `nvidia.com`, `github.blog` and more; every
  site under a brand top-level domain (`.google`, `.microsoft`, `.apple`...); a forum on the
  domain (`community.openai.com`) is not the organization speaking. `WEB_TOOLS_OFFICIAL_HOSTS`
  (comma-separated hosts or `host/path`) adds to the table.
- **An organization the query names:** its docs, developers, help, blog, newsroom, research and
  status pages are `official` (`docs.stripe.com`, `developers.notion.com`, `notion.so/help`) when
  the query says its name as a word or two joined, the name has four letters or more and is not
  a word that is only a word (`time`, `cloud`), and the page is the organization speaking: never
  an article that merely sits on a site with the name's word (`turkiye.ai/turkiye-yapay-zeka-...`
  stays `other`).
- **Aggregators** (`news.google.com`, `msn.com`, `flipboard.com`, `ground.news`, `techmeme.com`,
  `newsbreak.com`...; `WEB_TOOLS_AGGREGATOR_HOSTS` adds to them) are a new type, `aggregator`, and
  lose 0.12 in the score for every query.
- **`primary_first`** (on for a news query, a parameter) adds 0.12 to official sites and 0.10 to
  news outlets (0.05 academic) on top of their authority, unless the result is too old for the
  window the query asks about, so a fresh page still goes before a stale outlet.
- **"OpenAI Google Cloud GPT-6 Astra".** A query that names two organizations or more is about
  what holds both. The bonuses of a site's kind (authority, `primary_first`, the primary angle's)
  count in proportion to the share of the query's distinctive words that the result's title and
  snippet hold, and a result that holds more than half gets up to 0.12 more. The page about the
  model on `openai.com` (3 of 5 words) and Google Cloud's page on Astra (3 of 5) are official and
  went first; the Reuters piece with both organizations and the event (5 of 5) now goes before
  both, and the aggregator's copy of it is last (`tests/test_web_source_trust.py`). A query that
  names one organization or one project's documentation is untouched, and the ten recorded
  queries of `benchmarks/web_search_relevance.json` keep their order.

### What it measured live (2026-10-24, this Mac, one run each)

| | before (1.2.0) | after (1.3.0) |
|---|---|---|
| `AI news this week`, `discover`, 6 results (DuckDuckGo answered) | 5.5 s, 4,719 bytes; 1 result of 6 dated (a month, from its title) | 7.5 to 8.7 s, 2,786 to 2,955 bytes compact (5,500 full); date reads of 5 pages in 1.4 s: 3 read, 1 dated, 2 refused (Reuters, Bloomberg) |
| Reuters article, HTTP 401 `access_blocked` | error in 0.35 s; `archive: true`: "holds no archived copy" in 1.05 s | text of the same Reuters story from hindustantimes.com in 4.9 to 5.8 s, labelled as another outlet's, with 5 `alternates` (usnews, thestar, emirates247, aol, msn) and a `next_action` |
| Google Cloud blog post with a JSON-LD date and no snippet date | search `date: null`; scrape: no `date` (`datePublished` inside `metadata`) | scrape `date: 2024-05-15`, `date_confidence: 0.87`, `json_ld:datePublished`; search with `resolve_dates`: 4 of 5 Google Cloud posts dated in 1.0 to 1.5 s (one run in which the host answered slowly: `resolve_dates_cut:4` at the 4 s budget) |

The browsers (Chrome, Camoufox) were not installed in the throwaway environment of these runs; the
Reuters recovery did not need one.

### For Dot

- `resolve_dates` and recovery read addresses the server chooses. They are not bound by Dot's
  `[browser] allowed_hosts` and `blocked_hosts` (`web_search` with `read` is refused while the
  owner has set a list, for the same reason): set `WEB_TOOLS_SEARCH_RESOLVE_DATES=off` and
  `WEB_TOOLS_SCRAPE_RECOVER=off` in the environment Dot gives the server while a list is set.
- `web_scrape` has a new string argument, `content_id`, which the server works on and never
  sends (Dot's `STAYS_IN_THE_SERVER` list of the server's string arguments needs
  `web_scrape.content_id`); `web_health` takes none; the server has nine tools, not eight.
- The `web_scrape` answer is two text blocks (the JSON, then the Markdown); a client that parses
  `content[0]` as JSON finds `"markdown":"(below)"` where the page was.


## The server connects only to the address it checked (1.3.1, 2026-10-24)

An independent security review of Dot's item 623 found that the server checked a host and then connected
to a second lookup of it. `SafeFetcher._validate` ran `validate_outbound_url`, which resolved the name and
judged every address; then `client.stream("GET", url)` resolved the name again to connect, and so did each
redirect hop. A name whose DNS answer changes (a public address to the first lookup, `127.0.0.1` or a
192.168.x.x to the next: DNS rebinding) was checked as public and connected to as the loopback or the home
network. 1.3.0 had made that reachable without a request from the model:

- **Dates** (`web_tools/headdates.py`, `fetch_head`): a plain news `web_search` sent a blind GET to a local
  server, when one of the pages it found was on a name that rebinds.
- **Recovery** (`web_tools/service.py`): a walled `web_scrape` returned a local service's page, a token in it,
  as "another outlet's copy" (and the AMP and print forms and the feed it names are read the same way).

Two IPv6 forms passed the checks as well: `::127.0.0.1` and `::192.168.1.1` (IPv4-compatible), and
`64:ff9b::7f00:1` and `64:ff9b::c0a8:101` (NAT64). Dot's own client refused them.

### What changed

| | 1.3.0 | 1.3.1 |
|---|---|---|
| A connection of `SafeFetcher` | to a second lookup of the name | to an address of the one lookup, checked in that step (`PinnedTransport`, as Dot's `_CheckedBackend`); TLS and `Host` keep the name |
| A redirect hop | checked, then looked up again | checked and pinned again |
| The Internet Archive's curl session | the name checked by the validator, resolved again by libcurl | the transport resolves and checks it and gives libcurl those addresses as the only ones (`CURLOPT_RESOLVE`), no proxy from the environment, no answer from a private address |
| The older scrapers (`WebScraper`, `WebScraperAsync`) | validated, then libcurl resolved | pinned in the same way |
| `::127.0.0.1`, `64:ff9b::7f00:1`, `2002:c0a8:101::1` ... | accepted | judged as the IPv4 address they carry; refused when it is not public |
| `127.1`, `0177.0.0.1`, `2130706433` | accepted or resolved (libcurl reads `0177.0.0.1` as 127.0.0.1) | refused |
| A result link with `/url?` in it (Google) | requested to find the real address | only Google's own wrappers are |
| The headless browser | checked on the event loop, a yes kept 300 s | checked off the loop, a yes kept 30 s; what remains is its own name resolution |
| The older Playwright scraper | the first address only | its page's requests go through the browser's guard (1.3.1); its web sockets and the windows its page opens too (1.4.2) |
| `release.yml`, `ci.yml` | actions by tag, `pip install build twine` | actions by full commit with the release in a comment, `build==1.6.1`, `twine==7.0.0` |

`SafeFetcher(transport=...)` keeps its meaning: a transport of the caller's connects as it likes (a test's
mock, the archive's curl session, which pins itself) and the validator looks the name up as before. Without
one, the fetcher builds a `PinnedTransport` and its validator is `validate_outbound_url_syntax`, which looks
nothing up: a name is judged by what it resolves to, in the connection. A name that does not resolve is still
`dns_error` and one with an address that is not public `unsafe_target`; they arise when the connection is made.

### What it measured (the reviewer's probes, this Mac, `python -I`)

The probes patch name resolution (a public address to the first lookup of `rebind.test`, `127.0.0.1` to every
later one) and run a server on 127.0.0.1 that records what reaches it. 1.3.1 was run under `sandbox-exec` with
only the loopback open, since the public address of the probes is an invented one and the fix, rightly,
connects to it.

| Probe | 1.3.0 | 1.3.1 |
|---|---|---|
| `probe_rebind_e2e.py default` (a news search; dates and recovery on) | looked up 2 times; the local server saw `GET /admin/restart?x=1  Host: rebind.test:<port>` | looked up once; the local server saw nothing |
| `probe_rebind_recover.py default` (a walled scrape) | looked up 2 times; `recovered_from: other_source`, the answer held the owner-only text and `sk-internal-...`; the local service saw `GET /admin` | looked up once; the scrape fails with the wall's error; nothing reached the local service |
| both with the two switches off (`WEB_TOOLS_SEARCH_RESOLVE_DATES=off`, `WEB_TOOLS_SCRAPE_RECOVER=off`) | no lookup, nothing reached | the same |
| `SafeFetcher.fetch_head` of the rebinding name | answered 200 with the local page; looked up 2 times | refused to read the local page: the connection went to the public address (the sandbox denied it); looked up once |
| `validate_outbound_url` and `canonicalize_url` on `::127.0.0.1`, `::192.168.1.1`, `64:ff9b::7f00:1`, `64:ff9b::c0a8:101`, `64:ff9b::a9fe:a9fe` | accepted | refused |
| the same on `http://0177.0.0.1/` | accepted | refused |

The tests run the same attack in memory (`tests/rebinding_support.py`: a resolver that rebinds, a dialler that
serves public addresses from memory and, for any other address, connects for real to a recording server on the
loopback) for each path: a scrape, a redirect hop, the head read of `resolve_dates`, the AMP form, the feed,
another outlet's copy, the archive, the older scrapers and the browser's guard. They fail against 1.3.0 (the
local server is reached) and pass now (`tests/test_web_rebinding_paths.py`, `tests/test_web_pinned_connect.py`,
`tests/test_curl_pins.py`). The TLS test serves a certificate for a name from a local TLS server, reached through
a public address of the test, and checks the SNI, the `Host` header, and that a certificate for another name is
refused (`trustme`, in the `dev` extra).

### For Dot

- Nothing in Dot's configuration changes. `resolve_dates` and recovery still read addresses the server chooses
  and are still not bound by Dot's `[browser] allowed_hosts` and `blocked_hosts` (the 1.3.0 note above): that is
  about the owner's lists, not about rebinding, which no longer steers those reads to the local network.
- What the server does not pin is in [Security boundaries](#security-boundaries): the browsers' own resolution
  above all. A Dot that runs the browser fallbacks should know that a page it renders is checked at the moment
  of the render and not at the moment of the browser's connection.


## Round B: pages drawn by scripts, pages that are not there, and what the owner's test found (1.4.0, 2026-10-24)

The owner had Dot test the server and scored it 6.2/10 (`web_search` 7, `web_scrape` 5, `web_map` 9, `web_extract` 5,
`web_crawl` 5). Each finding was probed again on 1.3.1 (`WebToolsService.from_env()`, default settings) and is closed
below. What they praised stays: `web_map`'s sitemaps with `lastmod`, and the plain errors (a DNS error, a bot wall, a
provider's fallback said), and everything of 1.3.0 and 1.3.1: the dates, the healing providers, the walled pages, the
compact output, the source types, and every connection pinned to the address that was checked.

| Tool | Their score | What they found | What it was | What it is now |
|---|---|---|---|---|
| `web_search` | 7 | for "Model Context Protocol specification" mostly blogs and third parties | the official spec page, the project's blog and a copy of its site were all `other`; a blog post led, the spec page was fifth | the project's hosts are `official` (a copy on `.info` is not) and, for a query that asks for a specification, `primary_first` is on: the spec page leads |
| `web_scrape` | 5 | a blog post came back as "Loading animation while page loads"; `not a url at all` became `https://…` and a DNS error | the page's HTML is an empty `<div id="root">` and the comment `<!-- Loading animation while page loads -->`, which the Markdown reader printed as the page's text (34 characters); the browser was asked, read the page 500 ms after it loaded, saw nothing (0 characters) and the merge kept the longer shell; the request model prefixed every text with `https://` | the browser waits for the page to settle; a page that stays a shell says so; text that is no address says it is not a web address |
| `web_map` | 9 | 42 URLs, with `lastmod` | `senoldogan.dev` redirects to `www`, and the sitemap's `www` URLs were dropped as another host's: 1 URL | the host the address lands on is the site's own: 44 URLs, `host`, `redirected_from` |
| `web_extract` | 5 | `/bu-sayfa-yok-12345` (no such page) gave the home page's title as found; `data.title` was "X, X" | a soft 404, and a title written twice | the page says `soft_404` and `looks_missing` and no field is read from it; two signals that agree are one value |
| `web_crawl` | 5 | `max_pages: 5` showed `percent: 100` with 32 links in the queue | a crawl that ended said nothing of why | `stopped_by`, `not_visited`, and `queue_size` is 0 once it has ended |

### Pages drawn by scripts are waited for

**Over HTTP.** A page whose readable text is under `BROWSER_RENDER_MIN_CHARS` (200) characters while its HTML has a
`<script>` is read again in the headless browser: a loading placeholder ("Loading…", "Loading animation while page
loads", "Please enable JavaScript"), an empty `#root`, `#app` or `#__next`, or a few lines that a script is about to
replace. (1.3.1 read again only under 50 characters, or with `id="root"`/`id="app"` in the HTML. senoldogan.dev's
blog pages came over HTTP as an empty `#root` and a comment, and *were* read again: the 500 ms read of the browser
saw nothing, and a page the browser reads as shorter than the HTTP text keeps the HTTP text.) A page with no script is
never read again (nothing is about to draw it), and a page with 200 characters or more is a page.

**In the browser.** The read no longer waits a fixed 500 ms after `DOMContentLoaded`. After the `load` event:

1. the network is waited for to go quiet, at most 3 seconds (a page that never goes quiet is read all the same);
2. then the page's visible text is looked at every tenth of a second (the same choices the Markdown reader makes:
   the first of `main`, `article` and `role=main`, else the body, without menus, banners, footers, sidebars, forms and
   scripts) until it has not changed for `BROWSER_SETTLE_STABLE_MS` (600) **and** is more than a shell's: a loading
   placeholder (a line of it counts: a cookie notice above a "Loading…" is a shell with a notice) or next to nothing
   (under a quarter of `BROWSER_RENDER_MIN_CHARS`) keeps the wait going;
3. never sooner than `BROWSER_POST_LOAD_WAIT_MS` (500), and never later than `BROWSER_SETTLE_MAX_MS` (8,000) after the
   load, nor past `BROWSER_TIMEOUT_SECONDS` (30) for the whole render.

Text that shows up before the content and is long enough to be a page (a cookie notice, a banner: 50 characters or more)
counts as text: a page that shows only that when the network goes quiet is read as that, and is not called a shell.

Every request the browser makes is checked as in 1.3.1 (`RequestGuard`: the address of the host, a yes kept 30 s, a no
300 s), and since 1.4.2 so are the web sockets and the windows of the page; what the browser's own name resolution does
is still what [Security boundaries](#security-boundaries) says.
The wait is `timings.settle_ms` of the answer (`browser_ms` is the whole render), and the browser's own record says
why it ended: `stable`, or `cap` (the time ran out first).

**A page that blocks the browser (1.4.2).** A page whose script spins (`while (true) {}`) leaves `page.evaluate` and
`page.content` unanswered for ever. On 1.4.1 and before the render waited on them with nothing around it: with
`BROWSER_TIMEOUT_SECONDS` at 10 it was still going after 40 s, it kept one of the manager's context slots and, above it,
one of the MCP server's eight tool slots (eight such pages and the server answered nothing). Now the whole render is
bounded by `BROWSER_TIMEOUT_SECONDS` and 5 seconds, and ends with `timeout`: the service has the page the HTTP fetch
read and says `browser_render_failed:timeout` (a scrape of a page that is only drawn by scripts is `timeout`), its
context is closed whatever happened (a renderer stuck in a loop dies with its context), a context that does not close
in 5 seconds is given up and the browser that did not answer is shut down and started again by the next render, and the
shutdown of a browser is bounded as well. Tests: `tests/test_web_browser_hang.py`.

**A page that is still a shell says so.** After the read, a page whose text is a loading placeholder, an empty app root
or next to nothing, while its HTML has scripts, is not a short page: it is one that was not read.

| `warnings` | Meaning |
|---|---|
| `rendered_shell` | the browser read it, waited, and it is still a shell. `hint`: "The page is drawn by scripts and was still empty after the browser waited for it. It may need a login or more time (`WEB_TOOLS_BROWSER_SETTLE_MAX_MS`), or may not exist." |
| `needs_javascript` | no browser read it (it is off, or could not start: `browser_render_failed:…` says which); only the shell was read |

A page with either is not kept by the page cache or by the answer cache (asked again, it is read again; it may have
filled by then). A page the browser filled is kept as every page is.

**One loader.** `web_scrape`, `web_extract`, a crawl's pages, a map's start page and a search's `read` all read a page
through `WebToolsService._load_page`: the same HTTP read, the same decision to render, the same wait, the same
verdicts. Two tools never disagree on one page: the owner saw `web_scrape` return the shell and `web_extract` the home
page's title for one site. Fields are read from the page as it was rendered when it was rendered (what a person who
opened it sees). A search's `read` says `content_warnings` for a page it read that is a shell or a soft 404.
`web_map` now reads its start page through the loader, so a site drawn by scripts has the links of the page as drawn.

**What a browser read costs.** A page that is whole as it arrives is waited for about 1.2 s after it has loaded (the
500 ms the old wait was, the network at rest, and 600 ms of text that has stopped growing), a page whose script draws it
after 1.5 s is read at about 2.1 s, and a shell that never fills costs `BROWSER_SETTLE_MAX_MS`, once per call (it is not
cached). More pages go to the browser than before: any page with scripts and under 200 characters, so a short real page
with an analytics script pays the render (a few seconds the first time a browser starts, one or two after).

Tests: `tests/test_web_browser_settle.py` (the waiting against a fake page and a fake clock: late content, a shell that
never fills, the least and the most, the deadline, a network that never rests, a page that is navigating; and a real
headless browser against a real server on the loopback, skipped where there is none, as on the CI job, which does not
install Playwright) and `tests/test_web_js_pages.py` (what is a shell, `rendered_shell`, `needs_javascript`, the caches,
one loader, the settings, and `web_scrape` of a page a script draws after 1.5 s end to end with a real browser).

### Markup that is not text (after 1.4.0)

The Markdown reader printed every string the parser made of a page, and a comment is one: `<!-- Loading animation while
page loads -->` was the whole "page" of senoldogan.dev's blog pages when the app had not started (the words the owner
saw in `web_scrape`), and a Google Tag Manager comment, an IE conditional comment or a `<![CDATA[ ]]>` section was a
line of the Markdown of any page that has one (1.3.1 and 1.4.0 both did). A comment, a CDATA section, a processing
instruction, a declaration and a doctype are now left out of the Markdown, in a paragraph, a list, a table and a quote
alike; text that spells a comment (`&lt;!-- --&gt;` in a code sample) is text and stays. The shell the owner's pages
serve is now empty over HTTP (`empty_content`, and `needs_javascript` where no browser read it), and the page the
browser draws has no comment in it. Tests: `tests/test_web_markdown_comments.py`.

### Pages that do not exist (soft 404)

A site that answers every address with its home page or its app's shell gives a 200 for a page that is not there.
senoldogan.dev answers `/bu-sayfa-yok-12345` with the home page's `<title>`, no canonical link and the same shell as
every page; a real post has its own `<title>`, a canonical link to itself and `og:url`.

**Telling it.** A page is compared with the same host's answer to an address that cannot exist:

- **Which pages are asked about.** Only a page that may be a fallback: not the root of the site (the root is what a
  fallback shows), and either it does not say it is itself (no `<link rel=canonical>` in the server's own HTML, or one
  that names another address) or its title is the root's, as the root this session has read had it. A page that names
  itself is a page the site vouches for, and costs nothing. The canonical link is the one in the HTTP response, before a
  browser ran the page's scripts: a site's script can write a canonical link for any address it is asked for.
- **The probe.** `https://<host>/web-research-probe-<16 random hex digits>`, read once for each host and kept for the
  session (512 hosts; an answer that told nothing is asked again after 5 minutes), by the same loader and so by the same
  checks: the fetcher that pins its connections to the address it checked, and, for a host whose pages are drawn by
  scripts, the same browser and the same guard. Pages that want it at the same time wait for one probe.
  A 404 or 410 means the host says when a page is not there (nothing is flagged on it); any other refusal tells nothing.
- **The comparison.** The two documents, as the loader gave them (so a page drawn by scripts is compared as the browser
  drew it, not as the HTTP shell was), have the same title **and** the same text or nine words in ten alike
  (the words of the two addresses left out of both: "Nothing at /x" is still the fallback). The title is required
  because a site that knows a page writes its own into the HTML, so a real page whose script never filled it has a
  shell's text and its own title, and is not taken for a missing one. The text alone is not enough, and neither is the
  title alone: a site with one static `<title>` for all its pages is not a soft 404 site.
  The same text or content hash after rendering is the evidence; the title keeps a page the server vouches for apart.

**What the answer says.** `warnings` holds `soft_404`, `looks_missing: true`, and a `hint` ("This site answers every
address with the same page, so this address may not exist: what was read is the site's fallback, not a page of its
own."). `status_code` stays what the site said (200). `web_extract` returns no field read from the fallback: every
field is `null` with provenance `not_found` (a `required` one is reported as it is when the page has nothing), and its
`title`, `description`, `canonical_url` and `date*` are empty. `web_scrape` keeps the text it read, so that the caller
can see what the site gave. A crawl records a `soft_404` page as an error of the job (`failed` counts it as a 404 is)
and does not follow its links. The verdict is made before a page is kept, so a cached page says the same.

**What it costs.** One request to the same host, through the same checks, the first time a page that does not name
itself is read from a host (and one browser read when the host's pages are drawn by scripts); nothing for a page that
names itself or for the root. `WEB_TOOLS_SOFT_404_CHECK=false` never asks. A host that serves a real page for
everything, and a page with a canonical link to itself on a host whose root this session has not read, are taken at
their word: the check finds what it can find from one extra request and no more.

Tests: `tests/test_web_missing_pages.py` (a site that answers every path with its home page, a site that says 404, the
requests counted, once per host, an honest host, a failing probe, a framework that names every page itself, a page
drawn by scripts compared as drawn, a crawl) and `tests/test_web_missing_comparison.py` (the arithmetic).

### One value for two signals that agree

`data.title` was "X, X": a page that writes its `og:title` twice (a framework that renders its head from the layout and
from the page) gave two values, and the answer joined them. A field's values that read alike (the same words in the
same order, whatever the spacing and the case) are now one value, the first spelling, in the data and in the evidence;
two different values are still both there, and several authors or keywords still are. By name, a page's own JSON-LD
item (its `BlogPosting`) is read before the site's (its `Organization`, its `WebSite`) for `description` and `image` as
it is for `headline` and the dates: a page a browser has drawn carries the site's items ahead of the post's.

### Input that is no web address

`web_scrape("not a url at all")` became `https://not a url at all` in the request model (which prefixed every text with
`https://`) and failed as a DNS error. Now `senoldogan.dev` and `example.com/docs` get their `https://` as before, and
text that is no host stays what it was; the service then says, for every tool,

```
invalid_request: "not a url at all" is not a web address. Give a full address, such as https://example.com/page
```

(a text that is a host without its scheme is answered with "Did you mean https://…?"; `https://`, a host that is not a
name and a malformed URL are the same error). An address that is one and must not be used keeps the wording of the safety
refusals and its `unsafe_target`: `localhost`, a private address, a numeric host, credentials in the URL, and a scheme
that is not `http` or `https` (`ftp://`, `file:`, `javascript:`, `mailto:`). The older scraper endpoints
(`/api/v1/scrape`) keep their own wording. Tests: `tests/test_web_not_an_address.py`.

### A site whose address redirects

`web_map("https://senoldogan.dev")` returned one URL: the apex answers every path with a 308 to `www.senoldogan.dev`,
and the sitemap's `www` URLs were dropped as another host's. Now the host an address lands on is the site's own when it
is the same site (apex to `www`, `http` to `https`, one host of a registrable domain to another): the start page is
read first, through the loader, to learn where the address leads; robots.txt and the sitemaps are read there; the URLs
of the requested host and of the landed one are kept (with `include_subdomains`, the siblings of `www` as well); and
the answer says `host` and, when the address moved, `redirected_from`. When no page answers but robots.txt or the
sitemap does, the host they answered at is the site's. A crawl of the apex has the same scope: it follows the pages of
`www` (and reads a `use_sitemap` crawl's sitemap there) instead of ending at the first.

**A redirect does not decide which site is read (1.4.2).** 1.4.0 took whatever host the start page, robots.txt or a
sitemap landed on as the site: an address that forwarded every path to another site made `web_map` return that site's
addresses, a crawl read its pages (with `include_subdomains`, those of a host nothing had redirected to as well), and
a page that stayed put while only its robots.txt or sitemap forwarded elsewhere did the same. The landing is the site
only for the same registrable domain, with or without `www.`, over `http` or `https`. The names are compared by a short
table of the registries' second levels (`co.uk`, `com.au`, `co.za`, `com.sg`...) and of the hosts that hand a subdomain
to anyone (`github.io`, `vercel.app`, `blogspot.com`...), not by the Public Suffix List, so a registry it does not know
makes two of its hosts one site. Anything else is done as 1.3.1 did it: the one page the address leads to (a crawl reads
it and follows none of its links), and nothing of the other site's robots.txt, sitemaps or links is used or listed: a
file it answered is dropped, and so is a sitemap an index names that forwards to it. The fetcher still follows the
redirect of an address's `robots.txt` and `sitemap.xml`, two requests to the other site whose answers are dropped, and
the sitemaps they name are not asked for. The answer says `redirected_to`: where the address led, or the file that
forwarded (`https://c.test/robots.txt`). `web_crawl_status`, `web_crawl_results` and the job in the store say it too.
Tests: `tests/test_web_site_redirect.py`, `tests/test_web_site_boundary.py`.

### A crawl that has ended says why

| `stopped_by` | It means | `not_visited` |
|---|---|---|
| `max_pages` | the page limit was reached while links were waiting | the links waiting, and the ones past the depth limit |
| `max_depth` | nothing was waiting, but pages at `max_depth` had in-scope links the crawl would not follow | those links |
| `done` | everything in scope that the links led to was visited | 0 |
| `cancelled` | it was cancelled (a queued job, never started, has left its start page) | what was waiting |
| `interrupted` | the server stopped while it ran; it is not resumed | what was waiting |
| `failed` | the crawl itself failed (`errors` says) | what was waiting |

`stopped_by` is `null` while the crawl runs; `progress.queue_size` is the links waiting while it runs and 0 once it has
ended; `progress.not_visited` counts the links it left, and `web_crawl_results` lists the first 50 of them
(`not_visited_urls`) with `stopped_by` and the count. `percent` keeps its meaning (the share of the page limit that was
read: 100 for a crawl that ended). A restart keeps all of it (in the job's `progress` in the store; a job an older
version kept has no reason). Tests: `tests/test_web_crawl_stop.py`.

### Search: an organization's own site

**It is official.** A host whose registrable name is all the words of the query's subject joined (`modelcontextprotocol`
for "Model Context Protocol specification"), and its subdomains (its blog, its docs), is `official` for that query, on
any of its pages. The subject is the query without its stopwords, the words that ask for a kind of page (specification,
documentation, docs, API, reference, guide, tutorial, manual, install, release, version, latest, overview...) and its
numbers. It needs two words or more (a name of one word is the older rule's, below) and six letters, and it is not a
forum on the domain (`discuss.`, `community.`, `forum.`...). A lookalike on another top-level domain is not: the rule
holds on `com org io dev ai app co` (and `co.uk`, `com.au` and the other second levels of `com`, `co` and `org`), not on
`.info`, `.net`, `.xyz`, `.biz` and the rest, where a copy of a site is put.

**It leads.** `primary_first` is on, unless a call says, for a query that asks for a specification, documentation or an
API reference (`specification`, `spec`, `documentation`, `docs`, "API reference"; their Turkish, Finnish and German
words) and is not a news query. Only the organization's own pages lead (official +0.12, academic +0.05; an outlet that
writes about a specification is not the specification), and what leads is the document itself:

- an official page whose address says it is a document (a path segment or the host's first label: `specification`,
  `spec`, `docs`, `documentation`, `reference`, `api`, `manual`) gets +0.12, +0.12 more when it is the kind the query
  asked for (`/specification/` for "…specification");
- an official post about it (`blog`, `news`, `press`, `posts`, `announcements`...) does not get the primary bonus;
- an older edition of a versioned page the results have a newer one of (`/specification/2025-03-26` beside
  `/specification/2026-07-28`, `/3.11/` beside `/3.12/`) loses 0.10, unless the query has a number in it;
- officials are not held back by the diversity step that keeps one kind of source from filling the page.

`primary_first: true` is as it was (official +0.12, news +0.10, academic +0.05, whatever the query) and `false` is none.

**What it measured** on the pool Yahoo answered on 2026-10-24 (`tests/fixtures/search_model_context_protocol_specification.json`,
the three angles of a discover search), ranked by the two versions with the default `limit` of 5 and the clock of that
day (`tests/test_web_search_official_name.py` runs the 1.4.0 column):

| | 1.3.1 | 1.4.0 |
|---|---|---|
| 1 | `blog.modelcontextprotocol.io/posts/2026-07-28` (`other`) | `modelcontextprotocol.io/specification/2026-07-28` (`official`) |
| 2 | `modelcontextprotocol.info/specification` (`other`) | `blog.modelcontextprotocol.io/posts/2026-07-28` (`official`) |
| 3 | `modelcontextprotocol.io/docs/2026-07-28/learn/architecture` (`reference`) | `modelcontextprotocol.io/specification/2026-07-28/architecture` (`official`) |
| 4 | `github.com/modelcontextprotocol/...` (`code`) | `modelcontextprotocol.info/specification` (`other`) |
| 5 | `modelcontextprotocol.io/specification/2025-03-26` (`other`) | `blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate` (`official`) |

The specification page of the 2026-07-28 edition is not among 1.3.1's five at all; the project's own pages are `other`
or `reference` there, and the copy of its site is second. The live answers of the same call (the pool varies from run
to run) are in the table below.

### How each source type is decided

`source_type` is decided from the address, in this order; the first rule that holds names the type
(`web_scraper/web_tools/discovery.py`, `classify_source_type`, with the tables of `sources.py`).

| Type | Decided by |
|---|---|
| `official` | a host of the built-in official table (`docs.python.org`, `developer.mozilla.org`, `react.dev`...); a public body (`europa.eu`, `gov.uk`, the Finnish authorities) or a government suffix (`.gov`, `.mil`, `.int`, `.gov.xx`); a first-party site (`openai.com`, `cloud.google.com`, `apple.com/newsroom`..., every site under a brand top-level domain, and `WEB_TOOLS_OFFICIAL_HOSTS`; a forum on the domain is not); and, for a query, a host the query names as a word or two joined (its docs, developers, help, blog, newsroom, research and status pages only: `docs.stripe.com`, `notion.so/help`), or a host named as all of the query's subject joined (any page, on an organization's top-level domain: see above) |
| `academic` | `arxiv.org`, `nature.com`, the universities named in the table, `.edu`, `.edu.xx`, `.ac.xx` |
| `news` | the outlets named by hand (`sources.py`: wires, public broadcasters, papers of record, the business and technology press), never a site because its name sounds like news |
| `press_release` | the wires that publish companies' own announcements (`businesswire.com`, `prnewswire.com`...) |
| `social` | `x.com`, `facebook.com`, `linkedin.com`... (and YouTube, for a news query) |
| `reference` | Wikipedia, Britannica and the like; and a page on a host no table knows whose path says it is documentation (`/docs/`, `/documentation/`, `/api/`, `/reference/`, `/manual/`: mirrors copy another project's documentation) |
| `code` | GitHub, GitLab, Bitbucket, Codeberg, `*.github.io` |
| `community` | forums and Q&A (`reddit.com`, `stackoverflow.com`, `news.ycombinator.com`, `dev.to`, `medium.com`) and blog hosts (`substack.com`, `blogspot.com`, `wordpress.com`...) |
| `aggregator` | sites that collect other outlets' stories (`news.google.com`, `msn.com`, `techmeme.com`...; `WEB_TOOLS_AGGREGATOR_HOSTS` adds) |
| `other` | everything else |

A host matches itself and its subdomains. What a type is worth is `_AUTHORITY` in `discovery.py` (official 0.24, academic
0.20, news 0.14, reference 0.10, code 0.06, community 0.04, aggregator −0.12), and `primary_first` adds on top.

### What it measured live (2026-10-25, this Mac, one run each, default settings)

Both columns were run in the same session, one after the other, from folders of their own: 1.3.1 from a pristine
copy of its source, 1.4.0 from the branch (the owner's calls, through the MCP handler, `WebToolsService.from_env()`).
The pages of senoldogan.dev were read lightly (one run each, a crawl of 4 or 5 pages). `web_extract` ran after the
`web_scrape` of the same pages, so it was answered from the page cache that the scrape filled.

| Call | 1.3.1 | 1.4.0 |
|---|---|---|
| `web_search` "Model Context Protocol specification" (DuckDuckGo and its lite page bot-walled, Yahoo answered) | 5.6 s; 1 blog post `other`, 2 GitHub `code`, 3 Wikipedia `reference`, 4 the copy of the site `other`, 5 the architecture page `reference` | 6.1 s; 1 `modelcontextprotocol.io/specification/2026-07-28` `official`, 2 the project's blog post `official`, 3 its architecture page `official`, 4 the copy of the site `other`, 5 Wikipedia `reference` |
| `web_scrape` the post on `www` (en) | 1.7 s; `fetched_with: http`, 34 characters, "Loading animation while page loads" (a comment) | 2.9 s; `fetched_with: browser`, 4,602 characters, settled in 1.9 s |
| `web_scrape` the post on the apex (tr) | 1.2 s; 34 characters, the same | 2.4 s; 4,366 characters, settled in 1.8 s |
| `web_scrape` `/bu-sayfa-yok-12345` | 0.9 s; 200, the home page's title, 34 characters, no warning | 5.2 s (the probe of the host included); `soft_404`, `looks_missing: true`, a `hint` |
| `web_extract` the same page | `data.title` the home page's title twice ("X, X"), `description` its description | every field `null`, `title: ""`, `soft_404`, `looks_missing: true`, a `hint` |
| `web_extract` the real post | the post's title and description (from the HTTP head) | the same (from the page as drawn) |
| `web_scrape("not a url at all")` | `dns_error`, "Could not resolve host not%20a%20url%20at%20all" | `invalid_request`, "…is not a web address. Give a full address, such as https://example.com/page" |
| `web_map("https://senoldogan.dev")` | 1.8 s; 1 URL | 2.6 s; 44 URLs (42 of the sitemap, with `lastmod`, and the links of the home page as drawn), `host: www.senoldogan.dev`, `redirected_from` |
| `web_map("https://www.senoldogan.dev")` | 0.6 s; 42 URLs | 2.2 s; 44 URLs |
| crawl of `https://modelcontextprotocol.io/docs`, `max_pages: 5` | 2.0 s; `completed`, `percent: 100`, `queue_size: 32` | 3.0 s; `completed`, `stopped_by: max_pages`, `queue_size: 0`, `not_visited: 32` |
| crawl of `https://senoldogan.dev`, `max_pages: 4`, `max_depth: 1` | 1 page (`discovered: 1`: the 308 to `www` ended it) | 4 pages of `www`, `stopped_by: max_pages`, `not_visited: 28` |

The map takes longer because its start page is read through the loader, and senoldogan.dev's is drawn by scripts. On
1.3.1 the page that the browser read after 500 ms was empty (0 characters) for both blog posts, and the 34 characters
that came back were the comment. The browsers were Playwright 1.64 with the installed Google Chrome, headless.

### For Dot

- New fields in the answers: `looks_missing` and `hint` (`web_scrape`, `web_extract`, and each page of a batch;
  `content_warnings` on a search result that was read), `host` and `redirected_from` (`web_map`), `stopped_by` and
  `progress.not_visited` (`web_crawl_status`), `not_visited` and `not_visited_urls` (`web_crawl_results`), `warnings`
  on a crawl result that is a shell, `timings.settle_ms`. New warnings: `rendered_shell`, `needs_javascript`,
  `soft_404`; a crawl's `errors` can hold `soft_404`. A text that is no address is `invalid_request` (it was a
  `dns_error`).
- New settings: `WEB_TOOLS_BROWSER_SETTLE_MAX_MS`, `WEB_TOOLS_BROWSER_SETTLE_STABLE_MS`, `WEB_TOOLS_SOFT_404_CHECK`;
  `BROWSER_POST_LOAD_WAIT_MS` now means the least a page is waited for, and `BROWSER_RENDER_MIN_CHARS` is the bound of
  the browser read for every page with scripts.
- The missing-page check and the settle make requests the model did not ask for, to the host it asked about and to no
  other, through the same checks. `WEB_TOOLS_SOFT_404_CHECK=false` keeps the first from being made.
- The nine tools and their arguments are the same; the text a model reads grew by 428 characters (11,932 of the 12,000 the
  budget test allows).
