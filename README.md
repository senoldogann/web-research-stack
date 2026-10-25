# Web Research Stack / Web Araştırma Yığını

<div align="center">
  https://github.com/senoldogann/web-research-stack/raw/main/GITHUB-video.mp4
  <br/>
  <em>2-Minute Demonstration / 2 Dakikalık Tanıtım Videosu</em>
</div>

[English](#english) | [Türkçe](#türkçe)

---

## English

### Overview

**Web Scraper** is a powerful, production-ready web scraping and research tool with full-stack capabilities. It combines a Python FastAPI backend with a modern React Next.js frontend, featuring LLM-powered research capabilities with citation support.

### Install

The MCP server for web search and reading that Dot, the always-on agent for macOS, uses ships as a Python package, `dotagent-web-research` (the import package is `web_scraper`):

```bash
pip install dotagent-web-research
# or, as a command of its own
uv tool install dotagent-web-research
```

It installs two commands: `web-research-mcp`, the MCP server over stdio (tools `web_search`, `web_scrape`, `web_map`, `web_extract`, `web_health`, `web_crawl_start`, `web_crawl_status`, `web_crawl_results`, `web_crawl_cancel`), and `web-scraper`, the CLI and REST server described below. The headless-browser fallbacks are extras: `pip install "dotagent-web-research[camoufox]"` or `"[playwright]"`; `"[stealth]"` adds Camoufox for reading a page again after a site's bot check turned the headless Chrome away (below, "What 1.4.2 added").

#### The tools, and what 1.3.0 added to them

| Tool | New in 1.3.0 |
|---|---|
| `web_search` | `compact` (the default over MCP: per result `title, url, domain, date, date_confidence, source_type, snippet, score`, and a `run` block with `provider`, `warnings`, `fallback`), `debug` (adds facets, queries, timings, score parts), `resolve_dates` (reads the head of the top pages for their dates; on for news and date ranges), `strict_dates` (drops undated and low-confidence results and says how many), `primary_first` (official sites and mainstream news lead; on for news). Results carry `date_confidence` and `date_conflicts`; the source type `aggregator` ranks lower. Providers heal: a wall earns a 10-minute cooldown, healthy providers go first, and `BRAVE_SEARCH_API_KEY` / `SERPER_API_KEY` turn on Brave and Serper (asked first, DuckDuckGo behind). A deep search no longer cuts its primary-source angle at 5 seconds. |
| `web_scrape` | `date`, `date_source`, `date_confidence`, `date_conflicts` (every date signal of the page weighed); `recover` (a walled page is read through the archive, its AMP or print form, its feed or another outlet, and says which: `recovered_from`, `alternates`, `next_action`); `lean`; `content_id` with `offset` (reads on in a page that `max_chars` cut, from the cache); the page's Markdown comes as a text block of its own, with its line breaks. |
| `web_extract` | The same `date*` fields; a named `published_at` is held to them. |
| `web_health` | New: each search provider's state (`ok`, `degraded`, `cooling` with the time left, `no_key`), calls, `block_rate`, mean latency, last error. |
| `web_map`, `web_crawl_*` | Unchanged. |

#### What 1.4.0 added

| Tool | New in 1.4.0 |
|---|---|
| `web_scrape` | A page drawn by scripts is read in the headless browser once it has **settled** (the network at rest, then its visible text no longer growing: not after a fixed 500 ms), and a page that is still a shell says so (`warnings: ["rendered_shell"]` or `needs_javascript`, with a `hint`) instead of passing for a short page. A page the site gives for **any** address says `soft_404` and `looks_missing: true` (the page is compared with the host's answer to a path that cannot exist; one extra request to that host per session). Text that is no web address says `"…" is not a web address. Give a full address, such as https://example.com/page` (`invalid_request`). Markup that is not text (an HTML comment) is no longer printed as the page's text. |
| `web_extract` | One value for two signals that agree (`data.title` was "X, X"); a page that does not exist gives no field and says `looks_missing`; the page's own JSON-LD item is read before the site's. |
| `web_map` | A site whose address redirects (apex to `www`, `http` to `https`) is mapped at the host it lands on: its sitemap's URLs are kept; `host` and `redirected_from` say which. |
| `web_crawl_*` | A crawl that has ended says why (`stopped_by`: `max_pages`, `max_depth`, `done`, `cancelled`...), how many links it left (`progress.not_visited`; `web_crawl_results` lists the first 50) and has `queue_size` 0. |
| `web_search` | A host named as the query's subject (`modelcontextprotocol.io` for "Model Context Protocol specification") and its subdomains are `official`, a copy of the site on another top-level domain is not; `primary_first` is also on for a query that asks for a specification, documentation or an API reference, and the document itself (its newest edition) leads. |

The cost: a page read in the browser is waited for until it settles (about 1.2 s after it loads when it is whole, up to
`WEB_TOOLS_BROWSER_SETTLE_MAX_MS`, 8 s, for a shell that never fills), more pages go to the browser (any page with a script
and under 200 characters of text), and the missing-page check makes one request to the page's host. New settings:
`WEB_TOOLS_BROWSER_SETTLE_MAX_MS`, `WEB_TOOLS_BROWSER_SETTLE_STABLE_MS`, `WEB_TOOLS_SOFT_404_CHECK`.

#### What 1.4.2 added

When a site's bot check turns the server's headless Chrome away, the server reads the page once more in Camoufox, a Firefox made to look like a person's browser, if the package `camoufox` is installed. It is an optional extra: `pip install "dotagent-web-research[stealth]"`, then `python -m camoufox fetch` once. A plain install, Dot's included, never does it. It runs on a wall and only then: a captcha, an access-blocked page, HTTP 401, 403 or 429 over HTTP, or Chrome timing out on the page. A page read that way says `fetched_with: browser` and why (`http_access_blocked`, `http_403`...). 1.4.2 also stopped Chrome from saying it is automated (`--disable-blink-features=AutomationControlled`).

What it gets through and what it does not, measured on 2026-10-25 with a handful of page loads per site: Camoufox was given the page by reuters.com (DataDome) and zillow.com (PerimeterX), which walled Chrome with 401 and 403, and passed all 20 checks of bot.sannysoft.com; it was walled again by g2.com (a Cloudflare managed challenge, even after 12 s and 25 s of waiting) and etsy.com (DataDome). The Chrome flag lowered CreepJS's "headless" score from 67% to 33% and moved no wall.

What it costs: a page that meets a wall is read up to three times; the first read in Camoufox takes about 10 s (the browser starts with it); `python -m camoufox fetch` downloads, once, the browser (about 100 MB from GitHub, 2.3 GB on disk), a fingerprint model (GitHub) and an ad blocker (the latest, from addons.mozilla.org); and the browser makes calls of its own (its ad blocker's filter lists). A site's own terms of use still bind whoever reads it: passing its bot check does not make reading the page permitted. [docs/web-tools.md](docs/web-tools.md#reading-a-page-again-in-camoufox-142) has the table, the downloads and the calls; `WEB_TOOLS_BROWSER_STEALTH=false` turns it off.

#### What 1.4.3 changed

An independent review of 1.4.1 found six things, and the reading in Camoufox had to be held to the same rules. Each is closed, with a test that fails before it.

| Tool | New in 1.4.3 |
|---|---|
| `web_map`, `web_crawl_*` | A redirect no longer decides which site is read: the address it lands on is the site only for a `www.` flip, `http` to `https` or the same registrable domain. An address that forwards to another site gives the one page it leads to and says `redirected_to` (before, `web_map` listed the other site's addresses and a crawl read its pages). |
| `web_scrape`, `web_extract`, `web_map` | The browser's guard holds a page's web sockets and the windows it opens, as it held its requests; a page that blocks the browser ends the read with `timeout` in Chrome and in Camoufox (it hung for ever, with a context slot and a tool slot); a page `web_map` read first is judged for `soft_404` when it is scraped; a page is returned only if it ended at an address the guard allows. |
| `web_search` | Of the hosts that have the query subject's name on several top-level domains, only the one the providers rank highest is `official` (`modelcontextprotocol.io`, not `.dev`, `.app` or `.co`). |
| the reading in Camoufox | A page never causes a download any more: Camoufox starts only from what `python -m camoufox fetch` put on the machine, within 60 s, and its updates of Firefox and of its media plug-ins (calls to Mozilla and Google) are off. Its requests and windows are held as Chrome's are; its web sockets, a worker's requests and a redirect are not (Playwright cannot hold them in Firefox). It is the extra `dotagent-web-research[stealth]`, and `WEB_TOOLS_BROWSER_STEALTH=false` turns it off. |

Not held: a redirect the browser follows (the guard sees a request's first address only), a shared worker's requests, a web socket opened inside a Web Worker (Playwright does not route it) and the browser's own name resolution.
The `playwright` extra now asks for 1.48.

[CHANGELOG.md](CHANGELOG.md) has the decisions; [docs/web-tools.md](docs/web-tools.md) the settings.

**How Dot uses it.** Dot's package depends on this one, so a fresh Dot install brings it. Dot starts `web-research-mcp` as one of its MCP servers (named `web-research`) and its tools reach the agent as `mcp__web-research__web_search`, `mcp__web-research__web_scrape` and so on. In Dot's `config.toml` it is one table; [docs/web-tools.md](docs/web-tools.md) has it and the settings (`WEB_TOOLS_*`) the server reads.

```toml
[[mcp]]
name = "web-research"
command = "web-research-mcp"
```

### Features

- **Dual Interface**: CLI commands and REST API
- **Local Web Research Tools**: HTTP-first multi-angle search with field-weighted BM25, weighted angle consensus, freshness filters, caching and provider cooldown, plus scrape, map, background crawl, and structured extract, exposed over REST and a Dot-ready stdio MCP server (headless-only browser fallback)
- **LLM Research Tool**: AI-powered web research with citations
- **Async Architecture**: High-performance concurrent scraping
- **Production Ready**: Docker, monitoring (Prometheus/Grafana), rate limiting, circuit breaker
- **Security First**: SSRF protection, input validation, security headers. The server connects only to an address it checked in the same step: a host name is resolved once, every address must be public, and the connection goes to those addresses, never to a second lookup (so a DNS-rebinding answer cannot steer a read to the loopback or the home network); the IPv6 forms that carry a private IPv4 address are refused too. The headless browser's guard holds the first address of each request, the web sockets and the windows of a page (1.4.2). What this does not cover, the headless browser's own name resolution, a redirect it follows and a web socket opened inside a Web Worker among it, is in [docs/web-tools.md](docs/web-tools.md#security-boundaries).
- **Multi-language UI**: English and Turkish language support
- **Streaming Support**: Real-time research progress via Server-Sent Events (SSE)
- **Code Block Preservation**: Scraper preserves code blocks from source pages with syntax highlighting
- **Internationalized Status Messages**: Research progress messages adapt to query language (TR/EN)
- **Resilient JSON Parsing**: 3-stage recovery for LLM synthesis output (strict → repair → retry)

### Tech Stack

| Component | Technology |
|-----------|-------------|
| Backend | Python 3.9+, FastAPI, asyncio |
| Frontend | React 19, Next.js 15, TypeScript, Tailwind CSS |
| Database | SQLite (history & caching) |
| HTTP Client | httpx, curl_cffi |
| HTML Parsing | BeautifulSoup4, lxml |
| AI/ML | Ollama, OpenAI API compatible |

### Quick Start

```bash
# Clone the repository
git clone https://github.com/senoldogann/web-research-stack.git
cd web-research-stack

# Install dependencies
pip install -e .

# Start the API server
web-scraper serve

# Or use Docker
docker compose up --build
```

### Google search stays off your screen

Google no longer serves results to plain HTTP clients, and headless Chrome is caught within one request. A headed Chrome gets the real page, so the searcher starts Chrome itself, **minimises its window over CDP before the first search**, and keeps one browser and one tab for the process: ~2 s to start (a brief flash of a small window), then 0.4–1 s per search. If Chrome is missing or refused, **Camoufox** — a patched headless Firefox — takes over.

```bash
pip install -e ".[playwright]"   # the Chrome path
pip install -e ".[camoufox]" && python -m camoufox fetch   # the headless fallback
```

- `GOOGLE_ALLOW_VISIBLE_BROWSER=true` — leave the Chrome window on screen (debugging).
- `GOOGLE_USE_PLAYWRIGHT=false` — skip Google entirely and rely on DuckDuckGo.

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | API information |
| `/health` | GET | Health check |
| `/api/v1/metrics` | GET | Prometheus metrics |
| `/api/v1/tools` | GET | Tool manifest for LLM |
| `/api/v1/scrape` | POST | Scrape a single URL |
| `/api/v1/scrape/batch` | POST | Batch scrape multiple URLs |
| `/api/v1/tools/web-research` | POST | Run AI-powered research |
| `/api/v1/tools/web-research/stream` | POST | Stream research progress |
| `/api/v1/web/search` | POST | Discover URLs with a search adapter |
| `/api/v1/web/scrape` | POST | Scrape one page to Markdown, metadata, and links |
| `/api/v1/web/map` | POST | List URLs from sitemaps and page links |
| `/api/v1/web/extract` | POST | Extract structured fields with provenance |
| `/api/v1/web/crawl` | POST | Start a background crawl and return its job id |
| `/api/v1/web/crawl/{job_id}` | GET | Crawl progress and counters |
| `/api/v1/web/crawl/{job_id}/results` | GET | Paginated crawl results |
| `/api/v1/web/crawl/{job_id}/cancel` | POST | Cancel a queued or in-flight crawl |
| `/api/v1/web/stats` | GET | Web tool latency, cache, and browser state |
| `/api/v1/research` | POST | Legacy research endpoint |
| `/api/v1/research/stream` | POST | Legacy streaming research |

### Example: Research Request

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tools/web-research" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "query": "Latest developments in AI",
    "max_sources": 5,
    "deep_mode": false,
    "provider": "ollama"
  }'
```

### Documentation

- [Installation Guide](./docs/installation-tr.md#installation-guide)
- [User Guide](./docs/user-guide.md#english)
- [API Documentation](./docs/api.md#english)
- [LLM Integration](./docs/llm-tool-integration.md)
- [Local Web Tools](./docs/web-tools.md)
- [Production Deployment](./docs/production-deployment.md)

### License

MIT License

---

## Türkçe

### Genel Bakış

**Web Scraper**, güçlü ve üretim ortamına hazır bir web kazıma ve araştırma aracıdır. Python FastAPI backend ile modern React Next.js frontend'i birleştirir, alıntılı LLM destekli araştırma özelliklerine sahiptir.

### Kurulum

Dot'un (macOS için her zaman açık ajan) kullandığı web arama ve okuma MCP sunucusu bir Python paketi olarak gelir: `dotagent-web-research` (içe aktarılan paket `web_scraper`):

```bash
pip install dotagent-web-research
# veya kendi başına bir komut olarak
uv tool install dotagent-web-research
```

İki komut kurar: stdio üzerinden MCP sunucusu `web-research-mcp` ve aşağıda anlatılan CLI ile REST sunucusu `web-scraper`. Araçlar: `web_search`, `web_scrape`, `web_map`, `web_extract`, `web_health` (1.3.0: her sağlayıcının durumu), `web_crawl_start`, `web_crawl_status`, `web_crawl_results`, `web_crawl_cancel`. 1.3.0 tarihleri güvenilir kılar (`date_confidence`, `date_conflicts`; `resolve_dates`, `strict_dates`), engellenen sayfayı başka yoldan okur (`recover`: arşiv, AMP/yazdırma biçimi, besleme, başka yayıncı), çıktıyı küçültür (`compact`, `lean`, `content_id` ile `offset`) ve sağlayıcıları iyileştirir (10 dakikalık soğuma, isteğe bağlı Brave ve Serper anahtarları). Headless tarayıcı yedekleri ek pakettir: `pip install "dotagent-web-research[camoufox]"` veya `"[playwright]"`.

**1.4.0 şunları ekledi.** Betikle çizilen sayfa, tarayıcıda *oturduktan* sonra okunur (ağ sakinleşir, sonra görünen metin büyümeyi bırakır; sabit 500 ms değil) ve hâlâ boş bir kabuk olan sayfa kısa bir sayfa gibi görünmek yerine bunu söyler (`rendered_shell`, `needs_javascript`, bir `hint`). Sitenin *her* adrese verdiği sayfa `soft_404` ve `looks_missing: true` der (sayfa, sunucunun var olamayacak bir yola verdiği yanıtla karşılaştırılır; oturum başına o sunucuya bir ek istek); `web_extract` ondan alan okumaz. Web adresi olmayan bir metin "web adresi değil" der ve örnek bir adres verir (`invalid_request`). `web_extract` aynı değeri iki kez birleştirmez (`data.title` "X, X" idi). Adresi yönlendiren bir sitenin (`senoldogan.dev`, `www`'ya) `web_map`'i varılan sunucuda yapılır (`host`, `redirected_from`). Biten bir tarama nedenini söyler (`stopped_by`: `max_pages`, `max_depth`, `done`, `cancelled`), kaç bağlantıyı bıraktığını (`progress.not_visited`) ve `queue_size` 0 olur. Aramada, sorgunun konusu olan sunucu (`modelcontextprotocol.io`) ve alt alanları `official` sayılır, başka bir üst alandaki kopyası sayılmaz; şartname, belge ya da API başvurusu isteyen sorguda `primary_first` açılır ve belgenin kendisi (en yeni sürümü) öne geçer. Bedeli: tarayıcıda okunan sayfa oturana dek beklenir (bütün gelen sayfa için yaklaşık 1,2 sn; hiç dolmayan kabuk için en çok `WEB_TOOLS_BROWSER_SETTLE_MAX_MS`, 8 sn), daha çok sayfa tarayıcıya gider ve kayıp-sayfa denetimi sayfanın sunucusuna bir istek yapar. Yeni ayarlar: `WEB_TOOLS_BROWSER_SETTLE_MAX_MS`, `WEB_TOOLS_BROWSER_SETTLE_STABLE_MS`, `WEB_TOOLS_SOFT_404_CHECK`.

**1.4.2 şunları ekledi.** Bir sitenin bot denetimi sunucunun başsız Chrome'unu geri çevirdiğinde, `camoufox` paketi kuruluysa sunucu sayfayı bir kez daha Camoufox'ta okur: insan tarayıcısına benzetilmiş bir Firefox. İsteğe bağlı bir ektir: `pip install "dotagent-web-research[stealth]"`, ardından bir kez `python -m camoufox fetch`. Sade bir kurulum (Dot'unki dahil) bunu hiç yapmaz. Yalnızca bir duvara çarpınca çalışır: captcha, erişim engeli sayfası, HTTP üzerinden 401, 403 ya da 429, ya da Chrome'un sayfada zaman aşımına uğraması. Böyle okunan sayfa `fetched_with: browser` der ve nedenini söyler (`http_access_blocked`, `http_403`...). 1.4.2 ayrıca Chrome'un kendini otomatik diye bildirmesini durdurdu (`--disable-blink-features=AutomationControlled`). Neyi geçtiği ve neyi geçemediği, 2026-10-25'te site başına birkaç sayfa yüklemesiyle ölçüldü: Chrome'u 401 ve 403 ile geri çeviren reuters.com (DataDome) ve zillow.com (PerimeterX) Camoufox'a sayfayı verdi, bot.sannysoft.com'un 20 denetiminin 20'sini geçti; g2.com (Cloudflare yönetilen meydan okuması, 12 sn ve 25 sn beklemeye rağmen) ve etsy.com (DataDome) onu da geri çevirdi. Chrome bayrağı CreepJS'in "headless" puanını %67'den %33'e indirdi ve hiçbir duvarı oynatmadı. Bedeli: duvara çarpan sayfa üç kereye kadar okunur; Camoufox'ta ilk okuma yaklaşık 10 sn sürer (tarayıcı onunla birlikte başlar); `python -m camoufox fetch` bir kez şunları indirir: tarayıcı (GitHub'dan yaklaşık 100 MB, diskte 2,3 GB), bir parmak izi modeli (GitHub) ve bir reklam engelleyici (addons.mozilla.org'dan en yeni sürüm); tarayıcı ayrıca kendi çağrılarını yapar (reklam engelleyicisinin süzgeç listeleri). Bir sitenin kendi kullanım koşulları sayfayı kim okursa okusun onu bağlar: bot denetimini geçmek sayfayı okumayı izinli kılmaz. Tablo, indirmeler ve çağrılar için [docs/web-tools.md](docs/web-tools.md#reading-a-page-again-in-camoufox-142); `WEB_TOOLS_BROWSER_STEALTH=false` kapatır.

**1.4.3 şunları değiştirdi.** Bağımsız bir inceleme 1.4.1'de altı şey buldu ve Camoufox'ta okumanın da aynı kurallara bağlanması gerekti; her biri kapandı ve öncesinde başarısız olan bir testi var. Bir yönlendirme artık hangi sitenin okunacağını belirlemez: varılan adres yalnızca `www.` farkı, `http`'den `https`'e geçiş ya da aynı kayıtlı alan adı ise sitedir; başka bir siteye yönlendiren adres, gittiği tek sayfayı verir ve `redirected_to` der (önceden `web_map` öteki sitenin adreslerini listeler, tarama onun sayfalarını okurdu). Tarayıcının bekçisi bir sayfanın isteklerini tuttuğu gibi web soketlerini ve açtığı pencereleri de tutar; tarayıcıyı kilitleyen sayfa okumayı Chrome'da da Camoufox'ta da `timeout` ile bitirir (önceden sonsuza dek asılı kalır, bir bağlam ve bir araç yuvası tutardı); `web_map`'in önce okuduğu sayfa kazınınca `soft_404` için yargılanır; sayfa, vardığı adres bekçinin izin vermediği bir adresse döndürülmez. Aramada, konunun adını birkaç üst alanda taşıyan sunuculardan yalnızca sağlayıcıların en üste koyduğu `official` olur (`modelcontextprotocol.io`; `.dev`, `.app`, `.co` olmaz). Camoufox artık bir sayfa yüzünden indirilmez: yalnızca `python -m camoufox fetch`'in makineye koyduğundan, 60 sn içinde başlatılır; Firefox'un ve medya eklentilerinin güncelleme çağrıları (Mozilla ve Google'a) kapalıdır. İstekleri ve pencereleri Chrome'unkiler gibi tutulur; web soketleri, bir işçinin (worker) istekleri ve bir yönlendirme tutulmaz (Playwright Firefox'ta tutamıyor). Ek olarak `dotagent-web-research[stealth]` kurulur, `WEB_TOOLS_BROWSER_STEALTH=false` kapatır. Tutulmayanlar: bir Web Worker'ın içinde açılan web soketi ve tarayıcının kendi ad çözümlemesi. `playwright` eki artık 1.48 ister.

**Dot bunu nasıl kullanır.** Dot'un paketi buna bağlıdır; yeni bir Dot kurulumu bunu da getirir. Dot `web-research-mcp`'yi MCP sunucularından biri olarak (`web-research` adıyla) başlatır; araçlar ajana `mcp__web-research__web_search`, `mcp__web-research__web_scrape` gibi görünür. Ayrıntı ve ayarlar için [docs/web-tools.md](docs/web-tools.md).

### Özellikler

- **Çift Arayüz**: CLI komutları ve REST API
- **Yerel Web Araştırma Araçları**: Alan ağırlıklı BM25, ağırlıklı açı uzlaşması, güncellik filtreleri, önbellek ve sağlayıcı soğuması içeren çok açılı HTTP öncelikli arama; kazıma, haritalama, arka plan taraması ve yapılandırılmış çıkarım; REST ve Dot'a hazır stdio MCP sunucusu (yalnızca headless tarayıcı yedeği)
- **LLM Araştırma Aracı**: Alıntılı AI destekli web araştırması
- **Asenkron Mimari**: Yüksek performanslı eşzamanlı kazıma
- **Üretim Hazır**: Docker, izleme (Prometheus/Grafana), hız sınırlama, circuit breaker
- **Güvenlik Öncelikli**: SSRF koruması, giriş doğrulama, güvenlik başlıkları. Sunucu yalnızca aynı adımda denetlediği bir adrese bağlanır: ana makine adı bir kez çözülür, her adres genel (public) olmalıdır ve bağlantı o adreslere yapılır, ikinci bir sorguya değil (DNS rebinding yanıtı bir okumayı loopback'e ya da ev ağına yöneltemez); özel IPv4 adresi taşıyan IPv6 biçimleri de reddedilir. Başsız tarayıcının bekçisi bir sayfanın isteklerini, web soketlerini ve açtığı pencereleri tutar (1.4.2). Kapsamadığı şeyler (başsız tarayıcının kendi ad çözümlemesi ve bir Web Worker'ın içinde açılan web soketi dahil) [docs/web-tools.md](docs/web-tools.md#security-boundaries) içindedir.
- **Çok Dilli UI**: İngilizce ve Türkçe dil desteği
- **Yayın Akışı**: Sunucu Tarafından Gönderilen Olaylar (SSE) ile gerçek zamanlı araştırma ilerlemesi
- **Kod Bloğu Koruma**: Kaynak sayfalardan kod blokları söz dizimi vurgulamasıyla korunur
- **Uluslararasılaştırılmış Durum Mesajları**: Araştırma ilerleme mesajları sorgu diline göre uyarlanır (TR/EN)
- **Dayanıklı JSON Ayrıştırma**: LLM sentez çıktısı için 3 aşamalı kurtarma (katı → onarım → yeniden deneme)

### Teknoloji Yığını

| Bileşen | Teknoloji |
|---------|-----------|
| Backend | Python 3.9+, FastAPI, asyncio |
| Frontend | React 19, Next.js 15, TypeScript, Tailwind CSS |
| Veritabanı | SQLite (geçmiş ve önbellek) |
| HTTP İstemcisi | httpx, curl_cffi |
| HTML Ayrıştırma | BeautifulSoup4, lxml |
| AI/ML | Ollama, OpenAI API uyumlu |

### Hızlı Başlangıç

```bash
# Depoyu klonla
git clone https://github.com/senoldogann/web-research-stack.git
cd web-research-stack

# Bağımlılıkları yükle
pip install -e .

# API sunucusunu başlat
web-scraper serve

# Veya Docker kullan
docker compose up --build
```

### Google araması ekranda görünmez

Google artık düz HTTP istemcilerine sonuç döndürmüyor ve headless Chrome tek istekte yakalanıyor. Başlı (headed) Chrome gerçek sayfayı alıyor; bu yüzden arayıcı Chrome'u kendisi başlatıp **penceresini ilk aramadan önce CDP ile küçültüyor** ve süreç boyunca tek tarayıcı + tek sekme kullanıyor: başlatma ~2 sn (küçük bir pencere kısa süre görünür), sonrası arama başına 0,4–1 sn. Chrome yoksa ya da reddederse **Camoufox** — yamalı, headless Firefox — devralıyor.

```bash
pip install -e ".[playwright]"   # Chrome yolu
pip install -e ".[camoufox]" && python -m camoufox fetch   # görünmez yedek
```

- `GOOGLE_ALLOW_VISIBLE_BROWSER=true` — Chrome penceresi ekranda kalsın (hata ayıklama).
- `GOOGLE_USE_PLAYWRIGHT=false` — Google'ı tamamen atla, DuckDuckGo kullan.

### API Uç Noktaları

| Uç Nokta | Metod | Açıklama |
|----------|-------|----------|
| `/` | GET | API bilgileri |
| `/health` | GET | Sağlık kontrolü |
| `/api/v1/metrics` | GET | Prometheus metrikleri |
| `/api/v1/tools` | GET | LLM için araç manifestosu |
| `/api/v1/scrape` | POST | Tek URL kazıma |
| `/api/v1/scrape/batch` | POST | Toplu URL kazıma |
| `/api/v1/tools/web-research` | POST | AI destekli araştırma |
| `/api/v1/tools/web-research/stream` | POST | Araştırma ilerlemesi yayını |
| `/api/v1/web/search` | POST | Arama adaptörüyle URL keşfi |
| `/api/v1/web/scrape` | POST | Tek sayfayı Markdown, üst veri ve bağlantılara dönüştür |
| `/api/v1/web/map` | POST | Site haritaları ve sayfa bağlantılarından URL listele |
| `/api/v1/web/extract` | POST | Kaynak bilgisiyle yapılandırılmış alan çıkar |
| `/api/v1/web/crawl` | POST | Arka plan taraması başlat, iş kimliğini hemen döndür |
| `/api/v1/web/crawl/{job_id}` | GET | Tarama ilerlemesi ve sayaçları |
| `/api/v1/web/crawl/{job_id}/results` | GET | Sayfalanmış tarama sonuçları |
| `/api/v1/web/crawl/{job_id}/cancel` | POST | Sıradaki veya sürmekte olan taramayı iptal et |
| `/api/v1/web/stats` | GET | Web aracı gecikmesi, önbellek ve tarayıcı durumu |
| `/api/v1/research` | POST | Eski araştırma uç noktası |
| `/api/v1/research/stream` | POST | Eski yayın akışı araştırması |

### Örnek: Araştırma İsteği

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tools/web-research" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sizin-api-anahtariniz" \
  -d '{
    "query": "Yapay zeka son gelişmeler",
    "max_sources": 5,
    "deep_mode": false,
    "provider": "ollama"
  }'
```

### Dokümantasyon

- [Kurulum Rehberi](./docs/installation-tr.md#kurulum-rehberi)
- [Kullanım Kılavuzu](./docs/user-guide.md#türkçe)
- [API Dokümantasyonu](./docs/api.md#türkçe)
- [LLM Entegrasyonu](./docs/llm-tool-integration.md)
- [Yerel Web Araçları](./docs/web-tools.md)
- [Üretim Dağıtımı](./docs/production-deployment.md)

### Lisans

MIT Lisansı

---

## Contributing / Katkıda Bulunma

Contributions are welcome! Please read our [Contributing Guide](./CONTRIBUTING.md).

Katkılarınızı bekliyoruz! Lütfen [Katkı Rehberimizi](./CONTRIBUTING.md) okuyun.

---

&copy; 2026 Senol Dogan. MIT License.
