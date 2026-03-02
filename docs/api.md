# API Documentation / API Dokümantasyonu

[English](#english) | [Türkçe](#türkçe)

---

## English

### API Endpoints Overview

The Web Scraper provides a RESTful API built with FastAPI. All endpoints (except health check) require authentication when API keys are configured.

### Base URL

```
http://localhost:8000/api/v1
```

### Authentication

The API supports two authentication methods:

1. **API Key Header** (Recommended):
```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v1/tools
```

2. **Bearer Token**:
```bash
curl -H "Authorization: Bearer your-api-key" http://localhost:8000/api/v1/tools
```

---

### Endpoints

#### 1. Root

**GET** `/`

Returns API information and available endpoint paths.

**Response:**
```json
{
  "name": "Web Scraper API",
  "version": "1.1.0",
  "docs": "/docs",
  "endpoints": {
    "health": "/api/v1/health",
    "metrics": "/api/v1/metrics",
    "tools": "/api/v1/tools",
    "scrape": "/api/v1/scrape",
    "research_tool": "/api/v1/tools/web-research"
  }
}
```

---

#### 2. Health Check

**GET** `/health` or `/api/v1/health`

Check the health status of the API and its dependencies.

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2026-03-02T12:00:00+00:00",
  "dependencies": {
    "ollama": {
      "available": true,
      "host": "http://localhost:11434",
      "model": "gpt-oss:120b-cloud"
    },
    "circuit_breaker": {
      "open": false,
      "failure_count": 0
    },
    "cache": {
      "size": 12,
      "max_entries": 256
    },
    "history_store": {
      "total_entries": 42
    },
    "concurrency": {
      "active": 1,
      "max": 10
    }
  }
}
```

> **Note:** `status` returns `"healthy"` when Ollama is reachable and the circuit breaker is closed, or `"degraded"` otherwise.

---

#### 3. Prometheus Metrics

**GET** `/api/v1/metrics`

Returns Prometheus-formatted metrics for monitoring.

**Response:** `text/plain` (Prometheus exposition format)

Key metrics exported:

| Metric | Type | Description |
|--------|------|-------------|
| `web_scraper_requests_total` | counter | Total requests by method/endpoint/status |
| `web_scraper_request_duration_seconds` | histogram | Request duration in seconds by endpoint/method |
| `web_scraper_upstream_failures_total` | counter | Upstream (5xx) failures by reason/endpoint |
| `web_scraper_circuit_breaker_state` | gauge | Circuit breaker open (1) / closed (0) by breaker name |
| `web_scraper_active_requests` | gauge | In-flight requests |
| `web_scraper_cache_hits_total` | counter | Cache hit count |
| `web_scraper_cache_misses_total` | counter | Cache miss count |

---

#### 4. Tools Manifest

**GET** `/api/v1/tools`

Returns the available tools for LLM integration.

**Response:**
```json
{
  "tools": [
    {
      "name": "web_research",
      "description": "Run multi-source web research with citations.",
      "method": "POST",
      "path": "/api/v1/tools/web-research",
      "stream_path": "/api/v1/tools/web-research/stream",
      "auth": {
        "type": "api_key",
        "header": "X-API-Key"
      },
      "input_schema": {
        "type": "object",
        "properties": {
          "query": { "type": "string" },
          "max_sources": { "type": "integer" },
          "deep_mode": { "type": "boolean" },
          "research_profile": { "type": "string", "enum": ["technical", "news", "academic"] },
          "model": { "type": "string" },
          "provider": { "type": "string", "enum": ["ollama", "openai"] },
          "include_source_content": { "type": "boolean" }
        },
        "required": ["query"]
      },
      "output_schema": { "..." : "..." },
      "example": { "..." : "..." }
    }
  ]
}
```

---

#### 5. Scrape Single URL

**POST** `/api/v1/scrape`

Scrape content from a single URL.

**Request Body:**
```json
{
  "url": "https://example.com",
  "timeout": 30,
  "max_links": 100,
  "include_metadata": true,
  "include_links": true,
  "include_images": true
}
```

**Response:**
```json
{
  "url": "https://example.com",
  "title": "Example Domain",
  "content": "Extracted text content...",
  "metadata": {
    "description": "Page description",
    "keywords": "keyword1, keyword2"
  },
  "links": {
    "internal": [
      {"url": "https://example.com/page1", "text": "Link text"}
    ],
    "external": []
  },
  "images": [
    {"url": "https://example.com/image.jpg", "alt": "Image alt"}
  ],
  "status_code": 200,
  "response_time": 1.234
}
```

---

#### 6. Batch Scrape

**POST** `/api/v1/scrape/batch`

Scrape multiple URLs concurrently.

**Request Body:**
```json
{
  "urls": ["https://example.com", "https://example.org"],
  "timeout": 30,
  "max_concurrent": 5
}
```

**Response:**
```json
{
  "results": [
    {
      "url": "https://example.com",
      "title": "Example",
      "content": "...",
      "status_code": 200,
      "error": null
    }
  ],
  "count": 1
}
```

---

#### 7. Web Research

**POST** `/api/v1/tools/web-research`

Run AI-powered web research with citations.

**Request Body:**
```json
{
  "query": "Latest developments in AI",
  "max_sources": 5,
  "deep_mode": false,
  "research_profile": "technical",
  "model": "gpt-oss:120b-cloud",
  "provider": "ollama",
  "include_source_content": false,
  "openai_api_key": null
}
```

| Field | Type | Required | Description |
|-------|------|----------|--------------|
| `query` | string | ✅ | Research query (3-500 chars) |
| `max_sources` | integer | ❌ | Maximum sources to check (1-50) |
| `deep_mode` | boolean | ❌ | Enable deep research mode (default: false) |
| `research_profile` | string | ❌ | Source profile: `"technical"`, `"news"`, or `"academic"` (default: `"technical"`) |
| `model` | string | ❌ | Override default LLM model |
| `provider` | string | ❌ | `"ollama"` or `"openai"` (default: `"ollama"`) |
| `include_source_content` | boolean | ❌ | Include raw scraped text in sources (default: false) |
| `openai_api_key` | string | ❌ | OpenAI API key (when provider is `"openai"`) |

> **Research Profiles:** When `deep_mode` is enabled, the profile selects dedicated OSS collectors in addition to DuckDuckGo/Google:
> - `technical` — Wikipedia + StackExchange
> - `news` — HackerNews Algolia + Reuters/BBC/AP/AlJazeera RSS feeds
> - `academic` — arXiv + PubMed E-utilities

**Response:**
```json
{
  "query": "Latest developments in AI",
  "answer": "Synthesized answer with citations...",
  "summary": "Synthesized answer with citations...",
  "key_findings": [
    "Finding 1 with [1]",
    "Finding 2 with [2]"
  ],
  "detailed_analysis": "Long-form analysis...",
  "recommendations": "Actionable next steps...",
  "executive_summary": "High-level executive summary...",
  "data_table": [
    {
      "metric": "AI Market Size",
      "value": "$200B",
      "source": "Gartner",
      "date": "2026"
    }
  ],
  "conflicts_uncertainty": [
    "Source A says X while Source B says Y"
  ],
  "confidence_level": "High",
  "confidence_reason": "Multiple authoritative sources agree...",
  "citation_audit": {
    "total_citations": 4,
    "supported_citations": 3,
    "weak_citations": 1,
    "faithfulness_score": 0.75
  },
  "citations": [
    {
      "source": "docs",
      "url": "https://example.com/article",
      "title": "Article Title",
      "relevance_score": 0.91,
      "snippet": "Supporting excerpt...",
      "source_tier": 2,
      "publication_date": "2026-01-15"
    }
  ],
  "sources": [
    {
      "source": "docs",
      "url": "https://example.com/article",
      "title": "Article Title",
      "content": null,
      "relevance_score": 0.91,
      "error": null,
      "source_tier": 2,
      "publication_date": "2026-01-15"
    }
  ],
  "metadata": {
    "model": "gpt-oss:120b-cloud",
    "generated_at": "2026-03-02T12:00:00+00:00",
    "sources_checked": 5,
    "sources_succeeded": 4,
    "cached": false,
    "trace_id": "abc123-def456",
    "response_ms": 9123.55,
    "query_hash": "a1b2c3d4"
  }
}
```

**`citation_audit` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `total_citations` | integer | Number of `[N]` citation markers found in the synthesized answer |
| `supported_citations` | integer | Citations with sufficient keyword overlap with the referenced source |
| `weak_citations` | integer | Citations with low keyword overlap (faithfulness concern) |
| `faithfulness_score` | float | Fraction of supported citations (0–1). `1.0` when no citations exist |

> **Note:** For programming-related queries, `answer`, `detailed_analysis`, and `key_findings` may contain markdown fenced code blocks (`` ```language ... `` `).

---

#### 8. Streaming Research

**POST** `/api/v1/tools/web-research/stream`

Stream research progress in real-time using Server-Sent Events (SSE).

**Request Body:** Same as `/api/v1/tools/web-research`

**Response (SSE):**
```
data: {"type": "status", "message": "Starting research on: Latest AI developments"}

data: {"type": "status", "message": "Preparing search queries..."}

data: {"type": "status", "message": "Found 16 potential sources to check"}

data: {"type": "status", "message": "Gathering data from sources..."}

data: {"type": "status", "message": "Analyzing and synthesizing findings..."}

data: {"type": "result", ...full WebResearchResponse...}
```

> **Note:** Status messages are internationalized. If the query is in Turkish, messages appear in Turkish (e.g., `"Araştırma başlıyor: ..."`).

---

#### 9. Legacy Research Endpoints

**POST** `/api/v1/research` or `/api/research`

Backward-compatible research endpoint used by older clients. Same request body as web research.

**POST** `/api/v1/research/stream` or `/api/research/stream`

Legacy streaming research with backward-compatible response format.

---

### Rate Limits

| Endpoint | Limit |
|----------|-------|
| `/` | 60/min |
| `/health` | Unlimited |
| `/api/v1/scrape` | 60/min |
| `/api/v1/tools/web-research` | 15/min |
| `/api/v1/research` | 15/min |

---

### Error Responses

All error responses follow a consistent shape with `error`, optional `details`, and `trace_id`:

#### 400 Bad Request
```json
{
  "error": "Error description",
  "details": null,
  "trace_id": "abc123-def456"
}
```

#### 401 Unauthorized
```json
{
  "error": "Missing or invalid API key",
  "details": null,
  "trace_id": "abc123-def456"
}
```

#### 422 Validation Error
```json
{
  "error": "Validation failed",
  "details": [
    {
      "loc": ["body", "query"],
      "msg": "String should have at least 3 characters",
      "type": "string_too_short"
    }
  ],
  "trace_id": "abc123-def456"
}
```

#### 429 Too Many Requests
```json
{
  "error": "Rate limit exceeded",
  "retry_after_seconds": 30,
  "trace_id": "abc123-def456"
}
```

#### 500 Internal Server Error
```json
{
  "error": "Internal server error",
  "trace_id": "abc123-def456"
}
```

#### 503 Service Unavailable (Circuit Breaker)
```json
{
  "error": "Research backend temporarily unavailable",
  "retry_after_seconds": 30,
  "trace_id": "abc123-def456"
}
```

---

### Trace ID

Every response includes an `X-Trace-ID` header. You can also pass your own trace ID via the `X-Trace-ID` request header for distributed tracing.

---

---

## Türkçe

### API Uç Noktaları Genel Bakış

Web Scraper, FastAPI ile oluşturulmuş bir RESTful API sağlar. Sağlık kontrolü dışındaki tüm uç noktalar, API anahtarları yapılandırıldığında kimlik doğrulama gerektirir.

### Temel URL

```
http://localhost:8000/api/v1
```

### Kimlik Doğrulama

API iki kimlik doğrulama yöntemini destekler:

1. **API Anahtar Başlığı** (Önerilen):
```bash
curl -H "X-API-Key: sizin-api-anahtariniz" http://localhost:8000/api/v1/tools
```

2. **Bearer Token**:
```bash
curl -H "Authorization: Bearer sizin-api-anahtariniz" http://localhost:8000/api/v1/tools
```

---

### Uç Noktalar

#### 1. Kök

**GET** `/`

API bilgileri ve mevcut uç noktaları döndürür.

**Yanıt:**
```json
{
  "name": "Web Scraper API",
  "version": "1.1.0",
  "docs": "/docs",
  "endpoints": {
    "health": "/api/v1/health",
    "metrics": "/api/v1/metrics",
    "tools": "/api/v1/tools",
    "scrape": "/api/v1/scrape",
    "research_tool": "/api/v1/tools/web-research"
  }
}
```

---

#### 2. Sağlık Kontrolü

**GET** `/health` veya `/api/v1/health`

API'nin sağlık durumunu ve bağımlılıklarını kontrol eder.

**Yanıt:**
```json
{
  "status": "healthy",
  "timestamp": "2026-03-02T12:00:00+00:00",
  "dependencies": {
    "ollama": {
      "available": true,
      "host": "http://localhost:11434",
      "model": "gpt-oss:120b-cloud"
    },
    "circuit_breaker": {
      "open": false,
      "failure_count": 0
    },
    "cache": {
      "size": 12,
      "max_entries": 256
    },
    "history_store": {
      "total_entries": 42
    },
    "concurrency": {
      "active": 1,
      "max": 10
    }
  }
}
```

> **Not:** `status` alanı Ollama erişilebilir ve circuit breaker kapalıysa `"healthy"`, aksi halde `"degraded"` döner.

---

#### 3. Prometheus Metrikleri

**GET** `/api/v1/metrics`

İzleme için Prometheus formatında metrikler döndürür.

**Yanıt:** `text/plain` (Prometheus exposition formatı)

Dile getirilen temel metrikler:

| Metrik | Tip | Açıklama |
|--------|-----|----------|
| `web_scraper_requests_total` | counter | Metod/endpoint/durum koduna göre toplam istek |
| `web_scraper_request_duration_seconds` | histogram | Endpoint/metoda göre istek süresi (saniye) |
| `web_scraper_upstream_failures_total` | counter | Neden/endpoint'e göre upstream (5xx) hataları |
| `web_scraper_circuit_breaker_state` | gauge | Devre kesici açık (1) / kapalı (0) |
| `web_scraper_active_requests` | gauge | Devam eden istek sayısı |
| `web_scraper_cache_hits_total` | counter | Önbelleğe isabet sayısı |
| `web_scraper_cache_misses_total` | counter | Önbelleğ kaçırma sayısı |

---

#### 4. Araçlar Manifestosu

**GET** `/api/v1/tools`

LLM entegrasyonu için mevcut araçları döndürür.

**Yanıt:**
```json
{
  "tools": [
    {
      "name": "web_research",
      "description": "Alıntılarla çok kaynaklı web araştırması yap.",
      "method": "POST",
      "path": "/api/v1/tools/web-research",
      "stream_path": "/api/v1/tools/web-research/stream",
      "auth": {
        "type": "api_key",
        "header": "X-API-Key"
      },
      "input_schema": {
        "type": "object",
        "properties": {
          "query": { "type": "string" },
          "max_sources": { "type": "integer" },
          "deep_mode": { "type": "boolean" },
          "research_profile": { "type": "string", "enum": ["technical", "news", "academic"] },
          "model": { "type": "string" },
          "provider": { "type": "string", "enum": ["ollama", "openai"] },
          "include_source_content": { "type": "boolean" }
        },
        "required": ["query"]
      },
      "output_schema": { "..." : "..." },
      "example": { "..." : "..." }
    }
  ]
}
```

---

#### 5. Tek URL Kazıma

**POST** `/api/v1/scrape`

Tek bir URL'den içerik kazar.

**İstek Gövdesi:**
```json
{
  "url": "https://ornek.com",
  "timeout": 30,
  "max_links": 100,
  "include_metadata": true,
  "include_links": true,
  "include_images": true
}
```

**Yanıt:**
```json
{
  "url": "https://ornek.com",
  "title": "Örnek Alan Adı",
  "content": "Çıkarılan metin içeriği...",
  "metadata": {
    "description": "Sayfa açıklaması",
    "keywords": "anahtar1, anahtar2"
  },
  "links": {
    "internal": [
      {"url": "https://ornek.com/sayfa1", "text": "Bağlantı metni"}
    ],
    "external": []
  },
  "images": [
    {"url": "https://ornek.com/resim.jpg", "alt": "Resim açıklaması"}
  ],
  "status_code": 200,
  "response_time": 1.234
}
```

---

#### 6. Toplu Kazıma

**POST** `/api/v1/scrape/batch`

Birden fazla URL'yi eşzamanlı olarak kazar.

**İstek Gövdesi:**
```json
{
  "urls": ["https://ornek.com", "https://ornek.org"],
  "timeout": 30,
  "max_concurrent": 5
}
```

**Yanıt:**
```json
{
  "results": [
    {
      "url": "https://ornek.com",
      "title": "Örnek",
      "content": "...",
      "status_code": 200,
      "error": null
    }
  ],
  "count": 1
}
```

---

#### 7. Web Araştırması

**POST** `/api/v1/tools/web-research`

Alıntılarla AI destekli web araştırması çalıştırır.

**İstek Gövdesi:**
```json
{
  "query": "Yapay zeka son gelişmeler",
  "max_sources": 5,
  "deep_mode": false,
  "research_profile": "technical",
  "model": "gpt-oss:120b-cloud",
  "provider": "ollama",
  "include_source_content": false,
  "openai_api_key": null
}
```

| Alan | Tip | Zorunlu | Açıklama |
|------|-----|---------|----------|
| `query` | string | ✅ | Araştırma sorgusu (3-500 karakter) |
| `max_sources` | integer | ❌ | Kontrol edilecek maksimum kaynak (1-50) |
| `deep_mode` | boolean | ❌ | Derin araştırma modunu etkinleştir (varsayılan: false) |
| `research_profile` | string | ❌ | Kaynak profili: `"technical"`, `"news"` veya `"academic"` (varsayılan: `"technical"`) |
| `model` | string | ❌ | Varsayılan LLM modelini geçersiz kıl |
| `provider` | string | ❌ | `"ollama"` veya `"openai"` (varsayılan: `"ollama"`) |
| `include_source_content` | boolean | ❌ | Kaynaklarda ham kazınmış metin dahil et (varsayılan: false) |
| `openai_api_key` | string | ❌ | OpenAI API anahtarı (sağlayıcı `"openai"` olduğunda) |

> **Araştırma Profilleri:** `deep_mode` etkinleştirildiğinde profil, DuckDuckGo/Google'a ek olarak özel kolektörleri devreye sokar:
> - `technical` — Wikipedia + StackExchange
> - `news` — HackerNews Algolia + Reuters/BBC/AP/AlJazeera RSS beslemeleri
> - `academic` — arXiv + PubMed E-utilities

**Yanıt:**
```json
{
  "query": "Yapay zeka son gelişmeler",
  "answer": "Alıntılarla sentezlenmiş yanıt...",
  "summary": "Alıntılarla sentezlenmiş yanıt...",
  "key_findings": [
    "Bulgu 1 [1] ile",
    "Bulgu 2 [2] ile"
  ],
  "detailed_analysis": "Uzun biçimli analiz...",
  "recommendations": "Eyleme dönüştürülebilir sonraki adımlar...",
  "executive_summary": "Üst düzey yönetici özeti...",
  "data_table": [
    {
      "metric": "AI Pazar Büyüklüğü",
      "value": "200 Milyar $",
      "source": "Gartner",
      "date": "2026"
    }
  ],
  "conflicts_uncertainty": [
    "Kaynak A X derken Kaynak B Y diyor"
  ],
  "confidence_level": "High",
  "confidence_reason": "Birden fazla yetkili kaynak hemfikir...",
  "citation_audit": {
    "total_citations": 4,
    "supported_citations": 3,
    "weak_citations": 1,
    "faithfulness_score": 0.75
  },
  "citations": [
    {
      "source": "docs",
      "url": "https://ornek.com/makale",
      "title": "Makale Başlığı",
      "relevance_score": 0.91,
      "snippet": "Destekleyici alıntı...",
      "source_tier": 2,
      "publication_date": "2026-01-15"
    }
  ],
  "sources": [
    {
      "source": "docs",
      "url": "https://ornek.com/makale",
      "title": "Makale Başlığı",
      "content": null,
      "relevance_score": 0.91,
      "error": null,
      "source_tier": 2,
      "publication_date": "2026-01-15"
    }
  ],
  "metadata": {
    "model": "gpt-oss:120b-cloud",
    "generated_at": "2026-03-02T12:00:00+00:00",
    "sources_checked": 5,
    "sources_succeeded": 4,
    "cached": false,
    "trace_id": "abc123-def456",
    "response_ms": 9123.55,
    "query_hash": "a1b2c3d4"
  }
}
```

> **Not:** Programlama ile ilgili sorgularda `answer`, `detailed_analysis` ve `key_findings` markdown kod blokları içerebilir (`` ```dil ... ``` ``).

---

#### 8. Yayın Akışı Araştırması

**POST** `/api/v1/tools/web-research/stream`

Sunucu Tarafından Gönderilen Olaylar (SSE) kullanarak araştırma ilerlemesini gerçek zamanlı olarak yayınlar.

**İstek Gövdesi:** `/api/v1/tools/web-research` ile aynı

**Yanıt (SSE):**
```
data: {"type": "status", "message": "Araştırma başlıyor: Yapay zeka son gelişmeler"}

data: {"type": "status", "message": "Arama sorguları hazırlanıyor..."}

data: {"type": "status", "message": "Kontrol edilecek 16 potansiyel kaynak bulundu"}

data: {"type": "status", "message": "Kaynaklardan veriler toplanıyor..."}

data: {"type": "status", "message": "Bulgular analiz edilip sentezleniyor..."}

data: {"type": "result", ...tam WebResearchResponse...}
```

> **Not:** Durum mesajları uluslararasılaştırılmıştır. Sorgu Türkçe ise mesajlar Türkçe görünür; İngilizce ise İngilizce olarak gösterilir.

---

#### 9. Eski Araştırma Uç Noktaları

**POST** `/api/v1/research` veya `/api/research`

Eski istemciler tarafından kullanılan geriye uyumlu araştırma uç noktası. Web araştırması ile aynı istek gövdesi.

**POST** `/api/v1/research/stream` veya `/api/research/stream`

Geriye uyumlu yanıt formatında eski yayın akışı araştırması.

---

### Hız Sınırları

| Uç Nokta | Limit |
|----------|-------|
| `/` | 60/dak |
| `/health` | Sınırsız |
| `/api/v1/scrape` | 60/dak |
| `/api/v1/tools/web-research` | 15/dak |
| `/api/v1/research` | 15/dak |

---

### Hata Yanıtları

Tüm hata yanıtları `error`, opsiyonel `details` ve `trace_id` ile tutarlı bir yapıya sahiptir:

#### 400 Hatalı İstek
```json
{
  "error": "Hata açıklaması",
  "details": null,
  "trace_id": "abc123-def456"
}
```

#### 401 Yetkisiz
```json
{
  "error": "Eksik veya geçersiz API anahtarı",
  "details": null,
  "trace_id": "abc123-def456"
}
```

#### 422 Doğrulama Hatası
```json
{
  "error": "Doğrulama başarısız",
  "details": [
    {
      "loc": ["body", "query"],
      "msg": "Dize en az 3 karakter olmalıdır",
      "type": "string_too_short"
    }
  ],
  "trace_id": "abc123-def456"
}
```

#### 429 Çok Fazla İstek
```json
{
  "error": "Hız sınırı aşıldı",
  "retry_after_seconds": 30,
  "trace_id": "abc123-def456"
}
```

#### 500 Sunucu İçi Hata
```json
{
  "error": "Sunucu içi hata",
  "trace_id": "abc123-def456"
}
```

#### 503 Hizmet Kullanılamıyor (Circuit Breaker)
```json
{
  "error": "Araştırma sunucusu geçici olarak kullanılamıyor",
  "retry_after_seconds": 30,
  "trace_id": "abc123-def456"
}
```

---

### İzleme Kimliği (Trace ID)

Her yanıt bir `X-Trace-ID` başlığı içerir. Dağıtık izleme için kendi trace ID'nizi `X-Trace-ID` istek başlığı ile gönderebilirsiniz.
