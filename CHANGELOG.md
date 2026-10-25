# Changelog

The package is `dotagent-web-research`; the MCP server is `web-research-mcp`
(`web_scraper.mcp_server`). Behavior, settings and measurements are described in
[docs/web-tools.md](docs/web-tools.md); this file says what a version changed.

## 1.4.5 (2026-10-25)

An independent security review of 1.3.1 to 1.4.1 found six things in round B, and the reading of a page in Camoufox that
1.4.2 added had to be held to the same rules. 1.4.3 closes them, one commit each. Each has a test that fails before it:
the reviewer's probes are the scenarios, against sites in memory or, for the browsers, a real headless browser and
servers on the loopback (skipped where there is none, as on the CI job).

### Fixed

- **A redirect no longer decides which site a map or a crawl reads.** 1.4.0 took whatever host the start page,
  `robots.txt` or a sitemap landed on as the site. An address that forwarded every path to another site made `web_map`
  read that site's robots.txt and sitemaps and return its addresses, made a crawl read its pages and, with
  `include_subdomains`, a host nothing had redirected to (`internal.other.test`). A page that stayed put while only its
  robots.txt or sitemap forwarded elsewhere did the same, and so did a start page that was not there. A landing is the
  site only for a `www.` flip, `http` to `https` or the same registrable domain (`same_registrable_domain`: a short
  table of the registries' second levels and of the hosts that hand a subdomain to anyone, not the Public Suffix List).
  Any other is done as 1.3.1 did it: the one page the address leads to (a crawl reads it and follows none of its links),
  and nothing of the other site's robots.txt, sitemaps or links used or listed (the fetcher still follows the redirect
  of `robots.txt` and `sitemap.xml`, two requests whose answers are dropped, and a sitemap an index names that forwards
  elsewhere is dropped too), and the answer says `redirected_to`. It holds for a page that was read after a wall, in
  Chrome or in Camoufox: it goes by the address the page ended at.
- **The browser's guard holds the web sockets and the windows of a page.** It was routed on the one page: a page's
  `new WebSocket("ws://127.0.0.1:9222/...")` and `window.open("http://127.0.0.1:8080/...")` (the window's navigation,
  its image and its fetches) reached a host the guard refuses. It is now put on the context, before its first page
  exists: `context.route` for the requests (a window is a page of the context), `context.route_web_socket` for the
  sockets (an address that may be requested is connected as before, any other is closed with 1008), and a window is
  closed as it opens. The older Playwright scraper has the same. In Camoufox the requests and the windows are held and the
  web sockets are not (below).
- **A page that blocks the browser no longer hangs the render, its slots or the tool.** A page whose script spins
  (`while (true) {}`) leaves `page.evaluate` and `page.content` unanswered for ever; the render waited on them with
  nothing around it, kept a context slot and, above it, one of the MCP server's eight tool slots (with the manager's
  timeout at 10 s it was still going after 40 s). Each read, in Chrome or in Camoufox, is now bounded by
  `BROWSER_TIMEOUT_SECONDS` and 5 seconds and ends with `timeout` (the service has the HTTP text and says
  `browser_render_failed:timeout`); a page that blocks both browsers ends in about twice that. The context is closed
  whatever happened, for at most 5 seconds, and a browser that does not answer a close is shut down (Camoufox alone when
  it is the one) and started again by the next read. The shutdown of a browser is bounded too.
- **A page `web_map` read first is judged when it is scraped.** The map's start page is read without the missing-page
  question and was kept in the page cache; a scrape of it within five minutes got no `soft_404` and sent no probe, and a
  root the map had read was not remembered by its title. A read that wants the verdict and gets such a page from the
  cache asks it then and keeps the answer with the page (the page is not fetched or rendered again). It holds for a page
  that was read after a wall.
- **Only one host of a name is the subject's own.** A host whose registrable name is all the words of the query's
  subject joined was `official` on every top-level domain an organization uses: `modelcontextprotocol.dev`, `.app`,
  `.co` and `model-context-protocol.com` beside the project's `.io`. In a pool of results only one registrable domain of
  the name is: the one the tables name, else the one the providers ranked highest (the agreement of the angles on its
  best result). The others are `other`.
- **A page is returned only if it ended at an address the guard allows.** The guard is asked about the first address
  of a request; a redirect the browser follows is not asked about again, so an allowed address that answered
  `302 Location: http://127.0.0.1/...` was read from the loopback and handed back as the page. The address the page
  ended at is now asked about once more, and a refused one ends the read with `unsafe_target`. The request to the refused
  address is still made; what it answered is not returned.
- **Camoufox is no longer downloaded by a page.** It fetched what it lacked the moment it started, in the middle of a
  read (its browser and the model its fingerprints are drawn from from GitHub, its ad blocker, the latest, from
  addons.mozilla.org). It is now started only from what `python -m camoufox fetch` has put on the machine; when
  something is missing the page stays as Chrome read it, and the log says which command fetches the rest. Its start is
  bounded (60 s).
- **Camoufox's own calls to Mozilla and Google are off.** Within 25 s of each start it asked `aus5.mozilla.org`,
  `update.googleapis.com`, `ciscobinary.openh264.org`, `edgedl.me.gvt1.com` and `www.google.com` for updates (measured
  with a proxy that refused everything and wrote down what it was asked); none after. Its ad blocker's filter-list calls
  remain and are listed in [docs/web-tools.md](docs/web-tools.md).
- **`start()` looks at the first browser.** It returned early while only Camoufox was up, and the first read then found
  no browser.
- **The gate passes.** 1.4.2 as published failed `ruff check` (three findings) and `ruff format` (three files), its
  `tests/test_packaging.py` still pinned 1.4.1, and three of its tests needed the package `camoufox`.

### Added

- **`dotagent-web-research[stealth]`**, an extra that adds `camoufox>=0.5.8` (Python 3.10 or later) and leaves out its
  GeoIP database. `python -m camoufox fetch` then downloads, once, what it needs.
- **`WEB_TOOLS_BROWSER_STEALTH`** (true): `false` never reads a page again in Camoufox, installed or not.
- **`redirected_to`** in `web_map`, `web_crawl_status` and `web_crawl_results`: where an address led when that is another
  site.

### Stated

- `web_map`'s start page is read through the one loader, in the browser where the loader puts it (a page with little
  text and scripts). The browser resolves names itself, as it does for a scraped page: the guard is a check at one
  moment, and a name that answers a public address to it and another to the browser is the one thing it does not stop.

### Changed

- The `playwright` extra asks for 1.48 (it asked for 1.40): a Playwright that cannot route web sockets is not used
  (`browser_unavailable`), the guard does not run with a gap that is not the browser's own. Camoufox asks for a Playwright
  older than 1.63.
- The text of the nine tools a model reads is 11,998 characters (11,932 in 1.4.1; the budget test allows 12,000).

### What holds in Camoufox, and what does not

Measured with a real Camoufox 0.5.8 (Firefox 156, Playwright 1.62) and local servers
(`tests/test_web_browser_camoufox.py`):

| | Chrome | Camoufox |
|---|---|---|
| the requests of a page and of its frames | held | held |
| a window the page opens | closed as it opens | Firefox blocks it unless someone clicked; closed if not |
| the web sockets of a page | held | **not held** |
| the requests of a dedicated worker | held | **not held** |
| the requests of a shared worker | **not held** | **not held** |
| a web socket opened inside a worker | **not held** | **not held** |
| a redirect the browser follows | **not held**; the page it leads to is not returned | the same |
| an address the guard refuses | not read | not read, by either browser |

In Firefox Playwright puts its mock of `WebSocket` in the isolated world that Camoufox keeps Playwright's scripts in, so
the page's own `WebSocket` is the browser's. The tests that say what should hold are marked `xfail`.

### Measured (2026-10-25, this Mac, one run each, 1.4.1 against the branch before 1.4.2's Camoufox was merged into it)

The owner's calls of round B, through the MCP handler with default settings, to show that nothing round B gained is lost;
the last two rows are the new behavior on a real address that forwards to another registrable domain. They were not
repeated after the merge: the run after it was on the loopback only.

| Call | 1.4.1 | the branch |
|---|---|---|
| `web_search` "Model Context Protocol specification" | the specification page `official` first, then the architecture page and two blog posts `official` | the same specification page first and the same four `official` (Yahoo's order of the next two varies between runs) |
| `web_scrape` the blog posts of senoldogan.dev (en, tr) | `browser`, 4,602 and 4,366 characters, settled in 2.3 and 2.1 s | the same text, settled in 2.0 and 1.9 s |
| `web_scrape` / `web_extract` `/bu-sayfa-yok-12345` | `soft_404`, `looks_missing: true`, no field | the same |
| `web_scrape("not a url at all")` | `invalid_request` | the same |
| `web_map` of `senoldogan.dev` and `www.senoldogan.dev` | 44 URLs each, `host: www.senoldogan.dev`, `redirected_from` for the apex | the same (a `www.` flip is the same site) |
| crawl of `modelcontextprotocol.io/docs`, 5 pages; of `senoldogan.dev`, 4 pages, depth 1 | `stopped_by: max_pages`, `not_visited: 32`; 4 pages of `www`, `not_visited: 28` | the same |
| `web_map("https://wikipedia.com")` (forwards to `www.wikipedia.org`) | wikipedia.org's address, `host` and robots.txt (errors `blocked`, `http_error`) | `url` the address asked for, 1 URL, `redirected_to: https://www.wikipedia.org/`, nothing of wikipedia.org's robots.txt used |
| crawl of `https://wikipedia.com`, 3 pages | the page `https://www.wikipedia.org/` | the same one page, and `redirected_to` |

### Tests

Each fix has tests that fail before it: `tests/test_web_site_boundary.py` (an apex that forwards every path, with and
without `include_subdomains`, `use_sitemap` and a crawl; a page that stays while its robots.txt and sitemap forward; a
start page that is not there; a sitemap an index names that forwards; the REST routes; the `www.`, `http` and same-domain
moves that must stay; the registrable-domain table), `tests/test_web_browser_guard.py` (the handlers against fakes of what
Playwright hands them, and a real headless browser against two local servers), `tests/test_web_browser_hang.py` (a fake
page that never answers, the service, twelve calls at the MCP server's eight slots, and a real browser against the
spinning page), `tests/test_web_browser_landing.py` (a page that ended at the loopback is not returned),
`tests/test_web_missing_pages.py` and `tests/test_web_page_cache.py` (a map and then a scrape),
`tests/test_web_search_official_name.py` (the reviewer's lookalikes in one pool), and for the page read after a wall
`tests/test_web_browser_stealth.py` (fakes of Playwright and of the package camoufox: never a download, the start
bounded, the switch, the contexts, the bound of both browsers, twelve calls at eight slots with both blocked),
`tests/test_web_stealth_service.py` (a page read after a wall is judged for the landing and for `soft_404`) and
`tests/test_web_browser_camoufox.py` (a real Camoufox: what is held, what is not, a target that escalates to it).
On Python 3.11 with Playwright 1,890 tests pass, 9 are skipped (they need Camoufox) and 1 is expected to fail; on 3.10 and 3.12
without Playwright, as on the CI job, 1,881 pass and 19 are skipped; in a throwaway Python 3.13 environment with Playwright 1.62
and Camoufox 0.5.8, 1,896 pass and 4 are expected to fail (the gaps above).

### Not closed, and said so in [docs/web-tools.md](docs/web-tools.md)

- A redirect the headless browser follows: the guard is asked about the first address of a request only, and the
  browser follows a `Location` without asking again, so an allowed address that redirects to the loopback is requested
  from there (1.3.1 and 1.4.1 did the same; the pinned HTTP fetch refuses it). Since 1.4.3 the page a navigation was led
  to is not returned; the request is still made, and a script's fetch that is redirected still reaches the host. A
  shared worker's requests and the browser's UDP (WebRTC, WebTransport) are not held either.
- A web socket opened inside a Web Worker (dedicated or shared) is not routed by Playwright and reaches the host the
  guard would have refused: measured with `route_web_socket` on, the handshake of a worker reached the server. What
  closes it is a pinning proxy for the browser, not a longer list.
- In Camoufox the web sockets of a page, a worker's requests and a redirect hop are not held either (above).
- Ranking is all a pool has to tell a site from its lookalikes: one the engines put above the project's own is the one
  called official, and a name found on one domain only is official however it got there. A host classified alone, with
  no pool, is official as before.
- The same-site table does not know every registry: two hosts of one it does not know are taken for one site.
- Camoufox's ad blocker is left on, and makes the calls listed in the docs; taking it out could change what gets through,
  and that has not been measured.

## 1.4.4 (2026-10-25)

Published from pull request #13 (`a5fb038`). It changes how a search is made when DuckDuckGo's page cannot be used.

### Changed

- **Google is the first fallback of a search.** `WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS` defaults to `google, yahoo,
  duckduckgo_lite` (it was `duckduckgo_lite, yahoo, google`): a search whose first provider fails goes to Google, then to
  Yahoo, then to DuckDuckGo's lite page.
- **Google is read in a Camoufox of its own.** `web_scraper/stealth_camoufox.py` keeps one headless Camoufox for the whole
  process, with one browser context that every Google search reuses, and closes it after three idle minutes (looked at
  when the next search comes). It is a second Camoufox, apart from the one 1.4.2 added for reading a page after a bot check.
- **Google's consent wall is answered for the visitor.** The context is given two cookies when it is made,
  `CONSENT=YES+cb` and `SOCS=CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg`, for `.google.com` only. Both are
  written in the code and are the same for everyone: `SOCS` holds a Google web-server build (`gws_20230810-0_RC2`), a
  language (`en`) and a date (2023-08-16), and nothing that names a person or a machine. They are not read from any
  browser or profile, and they are kept in memory: measured with Camoufox 0.5.8, the browser's temporary profile has
  none of them on disk. A consent dialog that still shows is answered by clicking the first of these buttons it finds:
  "Accept all", "Reject all", "Tümünü kabul et", "Tümünü reddet", "Alle akzeptieren", "Hyväksy kaikki" (so "Accept
  all" where both are offered).
- **Date filters.** A search's `date_filter` (`day`, `week`, `month`, `year`) goes to Google as `tbs=qdr:d|w|m|y`.
- **Warm-up.** The first Google search opens `https://www.google.com/ncr` once before it, so that the search is faster; the
  REST server does the same in the background when it starts (`GOOGLE_WARMUP_ON_STARTUP`).


## 1.4.3 (2026-10-25)

Published from pull request #12 (`6b2e049`). It changes one thing: `camoufox` became a dependency of the package.

### Changed

- **Installing the package now installs Camoufox's Python package.** `camoufox>=0.5.8` is in `dependencies`, so
  `pip install dotagent-web-research`, and Dot, which installs it, bring it, with the packages it needs. The way a page
  is read did not change: Camoufox is used only when a site's bot check turns Chrome away (see 1.4.2). The package does
  not contain the browser, which Camoufox downloads the first time it is started, and it does not make a bot check
  pass: what it got through when checked is in the table under 1.4.2. What installing it brings, and what it costs, is
  under 1.4.4.


## 1.4.2 (2026-10-25)

Published from pull request #11 (`d3a3e70`). This entry says in plain words what it does and what was found when it was
checked; 1.4.3 changes some of it, and says where.

### Added

- **A page that meets a wall is read once more in Camoufox.** When a site's bot check turns the server's headless
  Chrome away, the server reads the page again in Camoufox, a Firefox made to look like a person's browser, if the
  package `camoufox` is installed (it is not a dependency of this package). It runs on a wall and only then: the plain
  HTTP read is answered with a captcha, an access-blocked page, or HTTP 401, 403 or 429 (`web_scrape` and the other tools
  sent only an access block to the browser before); Chrome reads a captcha or an access-blocked page; or Chrome
  times out or cannot navigate. `HeadlessBrowserManager` has `has_stealth` and `render_stealth()`, and `render()` goes on
  to Camoufox by itself. A page read that way says `fetched_with: browser` and why the plain read was refused
  (`http_access_blocked`, `http_captcha`, `http_403`...).
- **Chrome no longer says it is automated.** `--disable-blink-features=AutomationControlled` is one of its arguments, so
  `navigator.webdriver` is not true.

### What it gets through (measured on 2026-10-25, a handful of page loads per site)

| Check | Chrome, 1.4.1 | Chrome, 1.4.2 | Camoufox |
|---|---|---|---|
| bot.sannysoft.com | 20 of 20 pass | 20 of 20 | 20 of 20 |
| CreepJS "headless" / "like headless" | 67% / 44% | 33% / 44% | 0% / 0% |
| nowsecure.nl (Cloudflare) | page | page | page |
| reuters.com (DataDome) | 401 wall | 401 wall | page |
| zillow.com (PerimeterX) | 403 wall | 403 wall | page |
| g2.com (Cloudflare managed challenge) | 403 wall | 403 wall | 403 wall, with 12 s and with 25 s of waiting |
| etsy.com (DataDome) | 403 wall | 403 wall | 403 wall |

The flag lowers CreepJS's "headless" score and moves no wall. Camoufox was given the page by two of the four sites that
walled Chrome and was walled again by the other two. Single runs against sites that change.

### What it costs (found when 1.4.2 was checked)

- **A download by itself, the first time a page needed it.** Camoufox fetches what it lacks the moment it starts, in the
  middle of the read: its browser (about 100 MB, about 2.3 GB on disk) from the releases of `daijro/camoufox` on GitHub,
  the model its fingerprints are drawn from from the releases of `scrapfly/fingerprint-generator` on GitHub, and its ad
  blocker (the latest uBlock Origin, no checksum) from `addons.mozilla.org`. **1.4.3 stops this**: a page never causes a
  download; `python -m camoufox fetch` does, once, when you run it.
- **Calls of its own.** Camoufox calls Mozilla and Google by itself within 25 s of each start (updates of Firefox and of
  its media plug-ins), and its ad blocker fetches its filter lists from GitHub, GitLab, jsDelivr and others. **1.4.3
  switches the updates off**; the ad blocker's lists remain and are listed in the docs.
- **One more read, and time.** A page that meets a wall is read up to three times (HTTP, Chrome, Camoufox); the first read
  in Camoufox takes about 10 s because the browser starts with it.
- **A site's terms still bind.** A site that turns automated readers away has said something about them; passing its bot
  check does not make reading the page permitted.

### What it did not do (1.4.3 fixes it)

- It was not held like Chrome's page: no web sockets, no window, no time limit (a page that blocks the browser hung the
  render, once for each browser).
- The gate of the repository did not pass with it: `ruff check` found three things, `ruff format` three files, the
  version pin of `tests/test_packaging.py` still said 1.4.1, and three of its tests needed the package `camoufox`, which
  the CI job does not install.

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
