# Changelog

The package is `dotagent-web-research`; the MCP server is `web-research-mcp`
(`web_scraper.mcp_server`). Behavior, settings and measurements are described in
[docs/web-tools.md](docs/web-tools.md); this file says what a version changed.

## 1.4.2 (2026-10-25)

Bot evasion hardening and automatic challenge escalation.

### Added

- **Automatic stealth escalation for bot challenges & Turnstile.** `HeadlessBrowserManager`
  now supports stealth browsing via Camoufox (`has_stealth`, `render_stealth()`). When the primary
  engine (Chrome/Chromium) encounters a Cloudflare Turnstile, CAPTCHA, or challenge interstitial,
  or times out while blocked, the render seamlessly escalates to Camoufox without failing the request.
- **Bot challenge HTTP escalation.** In `service._fetch_document`, when an HTTP request receives a
  bot challenge (`captcha`, `access_blocked`, or HTTP 401/403/429) and a stealth-capable browser is
  available, `web_scrape` automatically escalates to headless browser rendering rather than
  terminating prematurely.

### Fixed

- **Blink automation flag.** Added `--disable-blink-features=AutomationControlled` to standard
  Chromium args, preventing the `navigator.webdriver = true` flag from triggering bot detection.

## 1.4.1 (2026-10-25)

1.4.0 reached PyPI from the first complete state of round B (`8d26b4b`), before its review and before the notes
below were written. 1.4.1 is round B as reviewed: what follows is in it and not in 1.4.0.

### Fixed

- **A comment is not the page's text.** The Markdown reader printed every string the parser made of a page, and a
  comment is one. senoldogan.dev's blog pages are served as an empty `<div id="root">` and
  `<!-- Loading animation while page loads -->`: the "Loading animation while page loads" that `web_scrape`
  returned was that comment (34 characters), and on 1.3.1 the browser, asked for the page, read it 500 ms after it
  loaded, saw nothing and the merge kept the shell. A comment, a CDATA section, a processing instruction, a
  declaration and a doctype are left out of the Markdown, in a paragraph, a list, a table and a quote alike (a
  Google Tag Manager or IE conditional comment inside a page's body was a line of its Markdown on 1.3.1 and 1.4.0).
  The shell the owner's pages serve now reads as empty (`empty_content`, `needs_javascript` where no browser read
  it) and the missing page's answer no longer shows the comment.
- **A probe of the missing-page check still going is cancelled when the service is closed** (it was left to the
  loop).
- **An edition is a year, a date, `v2` or a dotted number**: a bare number in an address (`/page/2`, `/45`) is a page
  number or an id, not an older edition of the page to be put under the newest (a specification query).

### Tests

- `tests/test_web_compact_output.py` freezes the day: its results say "Yesterday" next to an address dated
  2026/10/22, which agree on the 23rd only, and the test of the run block went red at midnight (on 1.3.1 too: the
  gate of `main` was red from 2026-10-25).
- The two tests that run a real browser against a local server no longer assert how long the wait was beyond what its
  outcome (`stable`, `cap`) already says.

## 1.4.0 — round B: pages that need JavaScript, pages that do not exist, and what the owner's test found

The owner had Dot test the server (2026-10-24) and scored it 6.2/10: `web_search` 7, `web_scrape` 5, `web_map` 9,
`web_extract` 5, `web_crawl` 5. Each finding was probed again on 1.3.1 with default settings and is closed below
(the measurements, with the pages of senoldogan.dev and modelcontextprotocol.io, are in
[docs/web-tools.md](docs/web-tools.md), "Round B"). What they praised stays: `web_map`'s sitemaps with `lastmod` and
the plain errors (a DNS error, a bot wall, a provider's fallback said), and everything of 1.3.0 and 1.3.1: the dates,
the healing providers, the walled pages, the compact output, the source types, and every connection pinned to the
address that was checked.

### Added

- **Pages drawn by scripts are waited for.** A page whose readable text is under `BROWSER_RENDER_MIN_CHARS` (200)
  characters while its HTML has a script (a loading placeholder, "Please enable JavaScript", an empty `#root`, `#app`
  or `#__next`, a few lines a script replaces) is read again in the headless browser. The browser no longer reads a
  page 500 ms after `DOMContentLoaded`: after `load` it waits for the network to go quiet (3 s at most), then until the
  page's visible text has stopped growing for 600 ms and is more than a shell's, no sooner than
  `BROWSER_POST_LOAD_WAIT_MS` and no later than `BROWSER_SETTLE_MAX_MS` (8 s) or the render's own timeout. Every
  request of the browser is checked as in 1.3.1. `timings.settle_ms` says how long the wait was.
- **A page that is still a shell says so.** `warnings: ["rendered_shell"]` (the browser read it and it is still a
  shell) or `needs_javascript` (no browser read it) and a `hint`: it is not a confident short page, and neither the
  page cache nor the answer cache keeps it.
- **One loader.** `web_scrape`, `web_extract`, a crawl's pages, a map's start page and a search's `read` read a page
  through `WebToolsService._load_page`: the same HTTP read, the same decision to render, the same wait, the same
  verdicts. Two tools never disagree on one page.
- **Pages that do not exist (soft 404).** A page that does not name itself (no `<link rel=canonical>` to its own
  address in the server's own HTML, or the title of the site's root) is compared with its host's answer to a random
  path (`/web-research-probe-<hex>`), asked once per host for the session through the same loader and the same
  checks; the same title and the same text (or nine words in ten) is `warnings: ["soft_404"]`, `looks_missing: true`
  and a `hint`. `web_extract` reads no field from it. A crawl records it as an error and does not follow its links.
- **`web_map` of a site whose address redirects** (apex to `www`, `http` to `https`): the host it lands on is the
  site's own. Its sitemaps are read there and their URLs kept; the answer says `host` and, when the address moved,
  `redirected_from`. A crawl of the apex follows the pages of `www`.
- **A crawl that has ended says why.** `stopped_by` (`max_pages`, `max_depth`, `done`, `cancelled`, `interrupted`,
  `failed`), `progress.not_visited` (the links left unvisited) and, in `web_crawl_results`, `not_visited_urls` (the
  first 50). `queue_size` is 0 once the crawl has ended.
- **Search: an organization's own site is official.** A host whose registrable name is all the words of the query's
  subject joined (`modelcontextprotocol` for "Model Context Protocol specification"), and its subdomains, is
  `official` for that query, on `com org io dev ai app co` (and `co.uk`, `com.au`...), not on `.info`, `.net`, `.xyz`.
  `primary_first` is also on for a query that asks for a specification, documentation or an API reference, and what
  leads is the document: an official page whose address says it is one gets +0.12 (twice for the kind asked for), an
  official post about it loses the bonus, and an older edition goes under the newest.
- **A text that is no web address says so.** `web_scrape("not a url at all")` and the other tools answer
  `invalid_request`: `"not a url at all" is not a web address. Give a full address, such as
  https://example.com/page` (a bare host such as `senoldogan.dev` still gets its `https://`). A real unsafe target
  keeps the wording of the safety refusals and its `unsafe_target`.
- `web_search` with `read` says `content_warnings` for a page it read that is a shell or a soft 404.

### Changed

- `web_extract` returns one value where two signals give the same one (`data.title` was "X, X"): values that read
  alike (the same words, whatever the spacing and the case) are one, the first spelling stays. By name, a page's own
  JSON-LD item (its `BlogPosting`) is read before the site's (`Organization`, `WebSite`) for `description` and `image`.
- `BROWSER_POST_LOAD_WAIT_MS` is the least a page is waited for (it was the whole wait). `BROWSER_RENDER_MIN_CHARS` is
  the bound of the browser read for every page with a script (1.3.1 read again under 50 characters, with `id="root"`
  or `id="app"` in the HTML, or with `document.write`).
- `web_map` reads its start page through the loader (a browser may start for a site drawn by scripts) and its `url` is
  the address the site landed on.
- The text of the nine tools a model reads is 11,932 characters (11,504 in 1.3.1; the budget test allows 12,000).

### Settings

`WEB_TOOLS_BROWSER_SETTLE_MAX_MS` (8000), `WEB_TOOLS_BROWSER_SETTLE_STABLE_MS` (600), `WEB_TOOLS_SOFT_404_CHECK`
(true). `.env.example` and the configuration table of the docs list them.

### What it costs

- A page read in the browser is waited for until it has settled: about 1.2 s after it loads for a page that is whole
  as it arrives, about 2.1 s for one whose script draws it after 1.5 s, and `BROWSER_SETTLE_MAX_MS` (once per call; a
  shell is not cached) for a shell that never fills. More pages go to the browser than before: any page with a script
  and under 200 characters of text.
- The missing-page check makes one request to the host of a page that does not name itself, the first time such a page
  is read from the host in a session (and one browser read when the host's pages are drawn by scripts); nothing for a
  page that names itself or for a root. `WEB_TOOLS_SOFT_404_CHECK=false` never asks.
- `web_map` of a site drawn by scripts takes a browser read of its start page (2.6 s against 1.8 s on senoldogan.dev).

### Measured (2026-10-25, this Mac, one run each, 1.3.1 against this branch, from folders of their own)

| Call | 1.3.1 | 1.4.0 |
|---|---|---|
| `web_search` "Model Context Protocol specification" | the project's blog post `other` first; the specification page not among the five | the specification page `official` first, then the project's blog post and its architecture page; the copy of the site (`.info`) `other` fourth |
| `web_scrape` a blog post of senoldogan.dev | 34 characters, "Loading animation while page loads" | 4,602 characters (4,366 for the Turkish post), settled in about 1.9 s |
| `web_scrape` / `web_extract` `/bu-sayfa-yok-12345` | 200, the home page's title (twice in `data.title`) | `soft_404`, `looks_missing: true`, no field |
| `web_scrape("not a url at all")` | `dns_error`, "Could not resolve host not%20a%20url%20at%20all" | `invalid_request`, "…is not a web address…" |
| `web_map("https://senoldogan.dev")` | 1 URL | 44 URLs, `host: www.senoldogan.dev` |
| crawl of `modelcontextprotocol.io/docs`, `max_pages: 5` | `completed`, `percent: 100`, `queue_size: 32` | `stopped_by: max_pages`, `queue_size: 0`, `not_visited: 32` |
| crawl of `senoldogan.dev`, `max_pages: 4`, `max_depth: 1` | 1 page | 4 pages of `www` |

### Tests

Each fix has tests against local servers and recorded pages, and each fails against 1.3.1: a page whose script draws
its content after 1.5 s, a shell that never fills (a real headless browser against a server on the loopback, skipped
where there is none, as on the CI job), a site that answers every path with its home page, an apex that redirects to
`www` with its sitemap on `www`, a crawl stopped by its page limit, and the ranking of the recorded search
(`tests/fixtures/search_model_context_protocol_specification.json`). On Python 3.11 with Playwright 1,751 tests pass; on 3.10 and 3.12 without it, as on the CI job, 1,749 pass and the two that run a real browser are skipped.

### Notes for Dot

- New fields: `looks_missing` and `hint` (`web_scrape`, `web_extract`), `host` and `redirected_from` (`web_map`),
  `stopped_by`, `progress.not_visited` (`web_crawl_status`), `not_visited` and `not_visited_urls`
  (`web_crawl_results`). The tools and their arguments are the same; no new string argument.
- The missing-page check and the wait make requests the model did not ask for, to the host it asked about and no other.
  Like `resolve_dates` and recovery, they are not bound by Dot's `[browser] allowed_hosts` and `blocked_hosts`.

## 1.3.1 — the server connects only to the address it checked

Found by an independent security review of Dot's item 623 (2026-10-24). Upgrade from 1.3.0.

### Fixed

- **DNS rebinding.** `SafeFetcher` checked a host by resolving its name (`validate_outbound_url`) and
  then let httpx resolve it again to connect, and so did each redirect hop. A name whose DNS answer
  changes (a public address to the first lookup, `127.0.0.1` or a 192.168.x.x to the next) was
  checked as public and connected to as the loopback or the home network. 1.3.0 made this reachable
  without a request from the model: a plain news `web_search` sent a blind GET to a local server
  (the dates read from the heads of results), and a walled `web_scrape` returned a local service's
  page, a token in it, as "another outlet's copy" (recovery). Now every connection goes to an
  address that was checked in the same step, as Dot's own client does: the name is resolved once,
  inside the connection (`web_scraper/pinned_transport.py`); every address of the answer must be
  public (one that is not refuses the name); the connection goes to one of them, IPv4 first; TLS
  (SNI and certificate) and the `Host` header keep the host name. Each redirect hop goes through
  the same step. This covers scrape, map, extract, crawl, `robots.txt`, sitemaps, the head reads of
  `resolve_dates`, and every recovery way: the AMP and print forms, the feed, another outlet's copy
  (all `SafeFetcher`), and the Internet Archive, whose curl session is given the checked addresses as
  the only ones the host has (`CURLOPT_RESOLVE`, `web_scraper/curl_pins.py`), without a proxy from the
  environment, and which does not return an answer that came from a private address. The older
  scrapers (`WebScraper`, `WebScraperAsync`: the CLI, the REST `/scrape`, the research agent) are
  pinned the same way.
- **IPv6 forms that carry a private IPv4 address.** `::127.0.0.1` and `::192.168.1.1`
  (IPv4-compatible), `64:ff9b::7f00:1` and `64:ff9b::c0a8:101` (NAT64) passed the checks. An IPv6
  address that carries an IPv4 address (IPv4-compatible, IPv4-mapped, IPv4-translated, NAT64
  `64:ff9b::/96`, 6to4 `2002::/16`) is now judged as that IPv4 address, in `validate_outbound_url`
  and in `canonicalize_url`; NAT64's local-use prefix `64:ff9b:1::/48` and the other IPv6 ranges that are
  not global unicast are refused whole. A public address carried in these forms is still allowed (an
  IPv6-only network's DNS64 answers NAT64 forms of public sites). IPv4 multicast is refused too.
- **A host written as a number** (`127.1`, `0x7f.0.0.1`, `0177.0.0.1`, `2130706433`) is refused. libcurl
  read `0177.0.0.1` as 127.0.0.1 where `getaddrinfo` may say 177.0.0.1, and a pin is never consulted for it.
- **Google's result links.** Any link that held `/url?` was requested to find the real address, a
  blind request to wherever a result pointed. Only Google's own redirect wrappers
  (`www.google.com/url?q=`, `/goto?url=`) are requested now.
- **The release workflow pins its actions by commit.** `actions/checkout`, `actions/setup-python`,
  `actions/upload-artifact`, `actions/download-artifact` and `pypa/gh-action-pypi-publish` (and the
  actions of `ci.yml`) are full commit SHAs with their release in a comment, and `build` and `twine` are
  installed at fixed versions (`build==1.6.1`, `twine==7.0.0`). `pypa/gh-action-pypi-publish` is v1.14.3,
  whose release notes name the fix for GHSA-5xjx-3w67-w9x9.

### Changed

- `SafeFetcher` makes its own connections through `PinnedTransport` unless it is given a transport; its
  default `validator` is then `validate_outbound_url_syntax` (everything the URL shows, no lookup). With a
  transport of the caller's, the validator is `validate_outbound_url` as before. A name that does not
  resolve is still `dns_error` and a private address `unsafe_target`; they now arise at the connection.
- The Internet Archive session and the older scrapers ignore `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY`
  (a proxy resolves the name itself); `SafeFetcher` always did.
- The headless browser's check of a page runs off the event loop; its verdict on a host is kept 30
  seconds for a yes (it was 300) and 300 for a no. The older Playwright scraper (`web-scraper
  --playwright`, the research agent's escalation) checked only the address it was given; its page's
  redirects and requests now go through the same `RequestGuard`. A browser cannot be pinned: what
  remains is its own name resolution (docs/web-tools.md, "Security boundaries").
- `trustme` joins the `dev` extra (a test of TLS through the pinned address).

## 1.3.0 — round A: dates you can trust, providers that heal, walled pages recovered, compact output

Made after a researcher used the server for a real job (the AI news of 16 to 23 October, 13
searches) and rated it 7/10. What they praised stays: `web_extract`'s provenance, `web_map`'s
`lastmod`, and the `warnings`, `timings`, `fallback`, `provider` and `provenance` that show how an
answer was got. Compact output moves them behind a flag; nothing is removed.

### Added

- **Dates you can trust.** One resolver reads every date signal of a page (JSON-LD
  `datePublished` and `dateModified`, `article:published_time`, `article:modified_time`,
  `og:updated_time`, the meta names `date`, `pubdate`, `publish-date`, `published_time`,
  `dc.date`, `parsely-pub-date`, microdata, `<time datetime>`, the date printed near the
  headline, the address) and weighs them. `web_scrape`, `web_extract` and every search result
  say `date`, `date_source`, `date_confidence` (0 to 1: signals within a day agree and raise it;
  an address-only or printed-only date is under 0.5) and `date_conflicts` (each disagreeing
  signal with its source and value); `date_modified` is its own field and never the published
  date. A named `published_at` in `web_extract` is held to the signals.
- **`resolve_dates`** (search): the first 64 KB of the top results' pages, read on to the end of
  the head up to 256 KB, within a 4-second budget, for results whose date is unknown or low;
  never a front page or a listing; on for news and date-range searches, off otherwise;
  `WEB_TOOLS_SEARCH_RESOLVE_DATES=off` turns it off for every call. **`strict_dates`** drops
  results with no date or a low `date_confidence` and says how many (`strict_dates_dropped:N`).
- **Providers that heal.** Each provider keeps a record in the process (answers, walls, HTTP
  errors, timeouts, exceptions, latency, last error). A wall starts a 10-minute cooldown at once,
  the third failure in a row of any other kind too (`WEB_TOOLS_SEARCH_PROVIDER_COOLDOWN_SECONDS`,
  `..._FAILURE_LIMIT`); the provider is skipped, then asked again on probation. Providers that
  are not cooling are asked by how their recent calls went.
- **Brave Search API and Serper**, on only with `BRAVE_SEARCH_API_KEY` or `SERPER_API_KEY`, asked
  first by a call that names no provider, DuckDuckGo and the chain behind them. The key is never
  in a log, an error, a URL, a repr or an answer.
- **`web_health`** (MCP tool; `GET /api/v1/web/health`): each provider's state (`ok`,
  `degraded`, `cooling` with the time left, `no_key`), calls, `block_rate`, `mean_latency_ms`,
  `last_error`, and the order the next search asks them in. No request is made.
- **Walled pages recovered.** A page that answers a captcha, a bot or login wall, 401, 403 or 429
  is read through the Internet Archive's copy, its AMP or print form, its feed, or another
  outlet's report found by its title, within 30 seconds. `recovered_from` says which, the text
  begins with a line saying whose it is, `alternates` lists other addresses (domain, title,
  date, source type, why) and `next_action` says what to do; with nothing readable the error
  carries them. `recover: false` turns it off, `WEB_TOOLS_SCRAPE_RECOVER=off` for every call.
- **Compact search.** `compact` (the default over MCP) returns per result only `title, url,
  domain, date, date_confidence, source_type, snippet, score` and a small `run` block
  (`provider`, `warnings`, `fallback`); `debug` adds the facets, queries, timings and score
  parts; `compact: false` is the whole answer, as before.
- **Lean scrape.** `lean` returns the text, its date and where it was cut. A page cut by
  `max_chars` says `truncated`, `total_chars`, `next_offset` and a `content_id`; `web_scrape`
  with `content_id` and `offset` reads the rest from the page cache, with no request.
- **The page in a block of its own.** The MCP answer of `web_scrape` is the JSON on its first
  line and the page's Markdown in a second text block, so a client that keeps a long answer in a
  file writes the page as Markdown with its own line breaks, not as one escaped line.
- **Source types that are right.** A known first-party domain is `official` (`openai.com`,
  `cloud.google.com`, `blog.google`, `anthropic.com`, `deepmind.google`, `ai.meta.com`,
  `microsoft.com`, the newsroom of `apple.com`, ...; `WEB_TOOLS_OFFICIAL_HOSTS` adds to the
  table), and so are an organization's own docs, blog, newsroom and developer pages for a query
  that names it. A new type, `aggregator` (`WEB_TOOLS_AGGREGATOR_HOSTS`), is penalised in the
  ranking. `primary_first` (on for news queries) puts official sites and mainstream news first;
  a query that names two organizations ranks the result that holds both and the event above the
  page about one of them.

### Changed

- The cooldown of a failed provider is 10 minutes after a wall (it was 5 seconds after any
  failure); the maximum is an hour.
- A deep search asks its primary-source angle first and lets it run to 15 seconds
  (`WEB_TOOLS_SEARCH_PRIMARY_BUDGET_MS`) while the side angles are cut at 5; which was cut is in
  `warnings` and in the angle's error.
- A walled page is read another way by default (it was an error with a hint); `archive: true`
  alone still asks only for the archive.
- A copy of a page read from the archive begins with a line that says it is the archive's.
- The tool schemas a model reads no longer allow `null` for an optional argument, and their
  descriptions are shorter: the tool text is 11,504 characters with the new tool and parameters
  (it was 11,451 with eight tools).
- `Yahoo`'s HTTP error status is `http_<status>` and a timeout is `search_timeout`, not the class
  name of an exception; Google's bot checks are `bot_detected`.
- An ISO moment with a zone written `+0300`, or a fraction of other than three or six digits,
  is read on Python 3.10 as on 3.11.

### Notes for Dot

- `resolve_dates` and walled-page recovery read addresses the server chooses (result pages, AMP
  and feed addresses of the same site, other outlets). They are not bound by Dot's
  `[browser] allowed_hosts` and `blocked_hosts`, as `web_search` with `read` is not: set
  `WEB_TOOLS_SEARCH_RESOLVE_DATES=off` and `WEB_TOOLS_SCRAPE_RECOVER=off` in the server's
  environment while the owner has set a host list.
- `web_scrape` takes one more string argument, `content_id` (a name the server gave a cached
  page, matching `^c-[0-9a-f]{12}$`); a test that lists the server's string arguments needs it.
  The server now has nine tools.

## 1.2.0

Search that holds up, reads walled pages, and ships as `dotagent-web-research`: see
[docs/web-tools.md](docs/web-tools.md), "Resilience and ergonomics (round 3)".
