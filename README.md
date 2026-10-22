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

### Features

- **Dual Interface**: CLI commands and REST API
- **LLM Research Tool**: AI-powered web research with citations
- **Async Architecture**: High-performance concurrent scraping
- **Production Ready**: Docker, monitoring (Prometheus/Grafana), rate limiting, circuit breaker
- **Security First**: SSRF protection, input validation, security headers
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

### Google search runs headless

Google no longer serves results to plain HTTP clients: a normal request gets a consent page or a JavaScript shell with nothing in it. Headless Chromium is detected within one request, so the searcher uses **Camoufox** — a patched Firefox with a coherent fingerprint — which Google answers with real results and **no window on screen**. One Camoufox stays alive for the process (≈6 s to start, ≈2 GB of memory) and closes itself after three idle minutes, so a search in a burst takes about a second.

```bash
pip install -e ".[camoufox]"   # the headless path
python -m camoufox fetch       # download the browser once
```

- `GOOGLE_USE_PLAYWRIGHT=false` — skip Google entirely and rely on DuckDuckGo.
- `GOOGLE_ALLOW_VISIBLE_BROWSER=true` — allow the Chrome fallback to open a real window (a server runs it under Xvfb; Docker sets this).
- `pip install -e ".[playwright]"` — needed only for that Chrome fallback.

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
- [Production Deployment](./docs/production-deployment.md)

### License

MIT License

---

## Türkçe

### Genel Bakış

**Web Scraper**, güçlü ve üretim ortamına hazır bir web kazıma ve araştırma aracıdır. Python FastAPI backend ile modern React Next.js frontend'i birleştirir, alıntılı LLM destekli araştırma özelliklerine sahiptir.

### Özellikler

- **Çift Arayüz**: CLI komutları ve REST API
- **LLM Araştırma Aracı**: Alıntılı AI destekli web araştırması
- **Asenkron Mimari**: Yüksek performanslı eşzamanlı kazıma
- **Üretim Hazır**: Docker, izleme (Prometheus/Grafana), hız sınırlama, circuit breaker
- **Güvenlik Öncelikli**: SSRF koruması, giriş doğrulama, güvenlik başlıkları
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

### Google araması görünmez pencerede çalışır

Google artık düz HTTP istemcilerine sonuç döndürmüyor: normal bir istek ya çerez onay sayfası ya da içinde sonuç olmayan bir JavaScript kabuğu dönüyor. Headless Chromium tek istekte yakalanıyor; bu yüzden arayıcı **Camoufox** kullanıyor — tutarlı bir parmak izine sahip yamalı Firefox. Google ona gerçek sonuçları veriyor ve **ekranda hiçbir pencere açılmıyor**. Tek Camoufox süreç boyunca açık kalıyor (başlatma ≈6 sn, bellek ≈2 GB) ve üç dakika boş kalınca kendini kapatıyor; böylece art arda aramalar ~1 saniye sürüyor.

```bash
pip install -e ".[camoufox]"   # görünmez yol
python -m camoufox fetch       # tarayıcıyı bir kez indir
```

- `GOOGLE_USE_PLAYWRIGHT=false` — Google'ı tamamen atla, sadece DuckDuckGo.
- `GOOGLE_ALLOW_VISIBLE_BROWSER=true` — Chrome yedeğinin gerçek pencere açmasına izin ver (sunucuda Xvfb altında; Docker bunu açar).
- `pip install -e ".[playwright]"` — sadece bu Chrome yedeği için gerekli.

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
- [Üretim Dağıtımı](./docs/production-deployment.md)

### Lisans

MIT Lisansı

---

## Contributing / Katkıda Bulunma

Contributions are welcome! Please read our [Contributing Guide](./CONTRIBUTING.md).

Katkılarınızı bekliyoruz! Lütfen [Katkı Rehberimizi](./CONTRIBUTING.md) okuyun.

---

&copy; 2026 Senol Dogan. MIT License.
