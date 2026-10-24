# Changelog

The package is `dotagent-web-research`; the MCP server is `web-research-mcp`
(`web_scraper.mcp_server`). Behavior, settings and measurements are described in
[docs/web-tools.md](docs/web-tools.md); this file says what a version changed.

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
