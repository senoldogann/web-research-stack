# Kurulum Rehberi / Installation Guide

[English](#installation-guide) | [Türkçe](#kurulum-rehberi)

---

## Installation Guide

### System Requirements

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| Python | 3.9+ | 3.11+ |
| Node.js | 18.0+ | 20.0+ |
| RAM | 4 GB | 8 GB+ |
| Disk | 1 GB | 5 GB+ |
| OS | Linux/macOS/Windows (WSL2) | Linux/macOS |

### Prerequisites

#### Backend (Python)

```bash
# Install Python 3.9+ if not already installed
# Check version
python3 --version

# Install uv package manager (recommended)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or use pip
pip --version
```

#### Frontend (Node.js)

```bash
# Install Node.js 18+ if not already installed
node --version

# Install npm
npm --version
```

---

### Installation Methods

#### Method 1: From Source

```bash
# Clone the repository
git clone https://github.com/senoldogann/web-research-stack.git
cd web-research-stack

# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .

# Or install with dev dependencies
pip install -e ".[dev]"

# Install playwright dependencies (optional)
pip install -e ".[playwright]"
playwright install chromium
```

#### Method 2: Docker (Recommended for Production)

```bash
# Clone and navigate to project
git clone https://github.com/senoldogann/web-research-stack.git
cd web-research-stack

# Start all services
docker compose up --build

# Or for production
docker compose -f docker-compose.production.yml up --build
```

---

### Environment Configuration

#### Backend Environment Variables

Create a `.env` file in the project root:

```bash
# .env

# API Configuration
API_HOST=127.0.0.1
API_PORT=8000
LOG_LEVEL=INFO
APP_ENV=production

# Security
API_KEYS=your-secret-api-key-here
API_ALLOWED_ORIGINS=http://localhost:3000
API_TRUSTED_HOSTS=localhost,127.0.0.1
API_MAX_REQUEST_BYTES=65536

# Rate Limits
API_RATE_LIMIT_PER_MINUTE=60
API_SCRAPE_RATE_LIMIT_PER_MINUTE=60
API_RESEARCH_RATE_LIMIT_PER_MINUTE=15
API_MAX_CONCURRENT_REQUESTS=10

# Ollama Configuration
OLLAMA_HOST=http://localhost:11434
DEFAULT_RESEARCH_MODEL=gpt-oss:120b-cloud

# Research Settings
RESEARCH_MAX_CONCURRENT_SOURCES=5
RESEARCH_TIMEOUT_PER_SOURCE=30.0
RESEARCH_SYNTHESIS_TIMEOUT_SECONDS=120.0
RESEARCH_DEEP_SYNTHESIS_TIMEOUT_SECONDS=240.0
RESEARCH_ENABLE_QUERY_REWRITE=true
RESEARCH_QUERY_REWRITE_MAX_VARIANTS=4
RESEARCH_ENABLE_GOOGLE_FALLBACK=true

# Scraper Settings
SCRAPER_TIMEOUT=30.0
SCRAPER_ALLOW_PRIVATE_NETWORKS=false
SCRAPER_MAX_RAW_TEXT_CHARS=50000

# Search Engine Timeouts
DUCKDUCKGO_REQUEST_TIMEOUT_SECONDS=30.0
DUCKDUCKGO_REQUEST_DELAY_SECONDS=0.5
GOOGLE_REQUEST_TIMEOUT_SECONDS=30.0

# Cache & History
CACHE_TTL_SECONDS=300
CACHE_MAX_ENTRIES=256
HISTORY_DB_PATH=web_scraper_history.sqlite3

# Circuit Breaker
CIRCUIT_BREAKER_FAILURE_THRESHOLD=5
CIRCUIT_BREAKER_RECOVERY_SECONDS=30
```

#### Frontend Environment Variables

Create `web-ui/.env.local`:

```bash
# web-ui/.env.local
NEXT_PUBLIC_BACKEND_URL=http://localhost:8000/api/v1
BACKEND_BASE_URL=http://localhost:8000
```

---

### Starting the Services

#### Backend Only

```bash
# Using CLI
web-scraper serve

# Or with custom settings
web-scraper serve --host 0.0.0.0 --port 8080

# Using Python directly
python -m uvicorn web_scraper.api:app --host 0.0.0.0 --port 8000 --reload
```

#### Frontend Only

```bash
cd web-ui
npm install
npm run dev
```

#### All Services (Docker)

```bash
docker compose up --build
```

This starts:
- Backend API: http://localhost:8000
- Frontend: http://localhost:3000
- FlareSolverr (for cloudflare bypass): http://localhost:8191

---

### Verification

#### Health Check

```bash
# Backend health
curl http://localhost:8000/health

# Expected response:
# {"status":"healthy","timestamp":"2026-01-01T00:00:00+00:00","dependencies":{...}}
```

#### API Tools Manifest

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v1/tools
```

---

### Troubleshooting

#### Port Already in Use

```bash
# Find process using port 8000
lsof -i :8000

# Kill the process
kill -9 <PID>
```

#### Ollama Connection Error

```bash
# Check if Ollama is running
curl http://localhost:11434/api/tags

# If not, start Ollama
ollama serve
```

#### Permission Errors

```bash
# Fix file permissions
chmod +x scripts/*.sh

# Or create new virtual environment
python3 -m venv new_venv
source new_venv/bin/activate
```

---

### Next Steps

- Read the [User Guide](./user-guide.md#english)
- Explore the [API Documentation](./api.md#english)
- Review the [LLM Integration Guide](./llm-tool-integration.md)

---

---

## Kurulum Rehberi

### Sistem Gereksinimleri

| Gereksinim | Minimum | Önerilen |
|------------|---------|----------|
| Python | 3.9+ | 3.11+ |
| Node.js | 18.0+ | 20.0+ |
| RAM | 4 GB | 8 GB+ |
| Disk | 1 GB | 5 GB+ |
| İşletim Sistemi | Linux/macOS/Windows (WSL2) | Linux/macOS |

### Ön Gereksinimler

#### Backend (Python)

```bash
# Python 3.9+ yüklü değilse yükle
# Sürümü kontrol et
python3 --version

# uv paket yöneticisini yükle (önerilen)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Veya pip kullan
pip --version
```

#### Frontend (Node.js)

```bash
# Node.js 18+ yüklü değilse yükle
node --version

# npm'i yükle
npm --version
```

---

### Kurulum Yöntemleri

#### Yöntem 1: Kaynaktan Kurulum

```bash
# Depoyu klonla
git clone https://github.com/senoldogann/web-research-stack.git
cd web-research-stack

# Sanal ortam oluştur (önerilen)
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Bağımlılıkları yükle
pip install -e .

# Geliştirme bağımlılıklarıyla yükle
pip install -e ".[dev]"

# Playwright bağımlılıklarını yükle (opsiyonel)
pip install -e ".[playwright]"
playwright install chromium
```

#### Yöntem 2: Docker (Üretim için Önerilen)

```bash
# Klonla ve projeye git
git clone https://github.com/senoldogann/web-research-stack.git
cd web-research-stack

# Tüm servisleri başlat
docker compose up --build

# Veya üretim için
docker compose -f docker-compose.production.yml up --build
```

---

### Ortam Yapılandırması

#### Backend Ortam Değişkenleri

Projenin kök dizininde bir `.env` dosyası oluşturun:

```bash
# .env

# API Yapılandırması
API_HOST=127.0.0.1
API_PORT=8000
LOG_LEVEL=INFO
APP_ENV=production

# Güvenlik
API_KEYS=sizin-gizli-api-anahtariniz
API_ALLOWED_ORIGINS=http://localhost:3000
API_TRUSTED_HOSTS=localhost,127.0.0.1
API_MAX_REQUEST_BYTES=65536

# Hız Sınırları
API_RATE_LIMIT_PER_MINUTE=60
API_SCRAPE_RATE_LIMIT_PER_MINUTE=60
API_RESEARCH_RATE_LIMIT_PER_MINUTE=15
API_MAX_CONCURRENT_REQUESTS=10

# Ollama Yapılandırması
OLLAMA_HOST=http://localhost:11434
DEFAULT_RESEARCH_MODEL=gpt-oss:120b-cloud

# Araştırma Ayarları
RESEARCH_MAX_CONCURRENT_SOURCES=5
RESEARCH_TIMEOUT_PER_SOURCE=30.0
RESEARCH_SYNTHESIS_TIMEOUT_SECONDS=120.0
RESEARCH_DEEP_SYNTHESIS_TIMEOUT_SECONDS=240.0
RESEARCH_ENABLE_QUERY_REWRITE=true
RESEARCH_QUERY_REWRITE_MAX_VARIANTS=4
RESEARCH_ENABLE_GOOGLE_FALLBACK=true

# Kazıyıcı Ayarları
SCRAPER_TIMEOUT=30.0
SCRAPER_ALLOW_PRIVATE_NETWORKS=false
SCRAPER_MAX_RAW_TEXT_CHARS=50000

# Arama Motoru Zaman Aşımları
DUCKDUCKGO_REQUEST_TIMEOUT_SECONDS=30.0
DUCKDUCKGO_REQUEST_DELAY_SECONDS=0.5
GOOGLE_REQUEST_TIMEOUT_SECONDS=30.0

# Önbellek ve Geçmiş
CACHE_TTL_SECONDS=300
CACHE_MAX_ENTRIES=256
HISTORY_DB_PATH=web_scraper_history.sqlite3

# Devre Kesici
CIRCUIT_BREAKER_FAILURE_THRESHOLD=5
CIRCUIT_BREAKER_RECOVERY_SECONDS=30
```

#### Frontend Ortam Değişkenleri

`web-ui/.env.local` dosyası oluşturun:

```bash
# web-ui/.env.local
NEXT_PUBLIC_BACKEND_URL=http://localhost:8000/api/v1
BACKEND_BASE_URL=http://localhost:8000
```

---

### Servisleri Başlatma

#### Sadece Backend

```bash
# CLI kullanarak
web-scraper serve

# Veya özel ayarlarla
web-scraper serve --host 0.0.0.0 --port 8080

# Python kullanarak
python -m uvicorn web_scraper.api:app --host 0.0.0.0 --port 8000 --reload
```

#### Sadece Frontend

```bash
cd web-ui
npm install
npm run dev
```

#### Tüm Servisler (Docker)

```bash
docker compose up --build
```

Bu şunları başlatır:
- Backend API: http://localhost:8000
- Frontend: http://localhost:3000
- FlareSolverr (cloudflare atlatma için): http://localhost:8191

---

### Doğrulama

#### Sağlık Kontrolü

```bash
# Backend sağlık kontrolü
curl http://localhost:8000/health

# Beklenen yanıt:
# {"status":"healthy","timestamp":"2026-01-01T00:00:00+00:00","dependencies":{...}}
```

#### API Araçları Manifestosu

```bash
curl -H "X-API-Key: sizin-api-anahtariniz" http://localhost:8000/api/v1/tools
```

---

### Sorun Giderme

#### Port Zaten Kullanımda

```bash
# 8000 portunu kullanan işlemi bul
lsof -i :8000

# İşlemi sonlandır
kill -9 <PID>
```

#### Ollama Bağlantı Hatası

```bash
# Ollama'nın çalışıp çalışmadığını kontrol et
curl http://localhost:11434/api/tags

# Çalışmıyorsa Ollama'yı başlat
ollama serve
```

#### İzin Hataları

```bash
# Dosya izinlerini düzelt
chmod +x scripts/*.sh

# Veya yeni sanal ortam oluştur
python3 -m venv new_venv
source new_venv/bin/activate
```

---

### Sonraki Adımlar

- [Kullanım Kılavuzu](./user-guide.md#türkçe) okuyun
- [API Dokümantasyonu](./api.md#türkçe) keşfedin
- [LLM Entegrasyon Rehberi](./llm-tool-integration.md) inceleyin
