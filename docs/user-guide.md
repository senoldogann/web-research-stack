# User Guide / Kullanım Kılavuzu

[English](#english) | [Türkçe](#türkçe)

---

## English

### Table of Contents

1. [Getting Started](#getting-started)
2. [CLI Usage](#cli-usage)
3. [Web Interface](#web-interface)
4. [Research Features](#research-features)
5. [Advanced Configuration](#advanced-configuration)

---

### Getting Started

After [installation](./installation-tr.md#installation-guide), you can use the application in three ways:

1. **Web Interface** (Recommended for beginners)
2. **CLI** (For developers and automation)
3. **REST API** (For integration with other systems)

---

### CLI Usage

#### Scrape a Single URL

```bash
# Basic usage
web-scraper scrape https://example.com

# Save to file
web-scraper scrape https://example.com -o result.json

# Pretty print JSON
web-scraper scrape https://example.com --pretty

# Custom timeout (in seconds)
web-scraper scrape https://example.com --timeout 60

# Custom max links
web-scraper scrape https://example.com --max-links 200
```

#### Start the API Server

```bash
# Default settings
web-scraper serve

# Custom host and port
web-scraper serve --host 0.0.0.0 --port 8080

# Development mode with auto-reload
web-scraper serve --reload

# Enable debug logging
web-scraper serve --log-level DEBUG
```

#### Batch Scraping

```bash
# Scrape multiple URLs
web-scraper batch https://example.com https://example.org -o results.jsonl

# Custom concurrency (default: 5)
web-scraper batch url1 url2 url3 -o results.jsonl --concurrent 10
```

---

### Web Interface

The web interface provides a modern UI for research tasks.

#### Access

Open your browser and navigate to: **http://localhost:3000**

#### Features

1. **Research Query**: Enter your research question
2. **Deep Mode**: Toggle for comprehensive research
3. **Settings**:
   - AI Provider selection (Ollama/OpenAI)
   - Model selection
   - API key configuration
   - Theme (Light/Dark/System)
   - Language (English/Turkish)

#### Screenshot

```
┌─────────────────────────────────────────────┐
│  🔍 Research Query                          │
│  ┌─────────────────────────────────────────┐│
│  │ What are the latest AI developments?   ││
│  └─────────────────────────────────────────┘│
│                                             │
│  [⚡ Deep Mode]  [🔍 Research]              │
│                                             │
│  Settings (Theme | Language | AI Provider)  │
└─────────────────────────────────────────────┘
```

---

### Research Features

#### Normal Mode vs Deep Mode

| Feature | Normal Mode | Deep Mode |
|---------|-------------|-----------|
| Max Sources | 5-15 | 15-50 |
| Content per Source | 2,500 chars | 8,000 chars |
| Analysis Depth | Summary | Comprehensive |
| Timeout | 120s | 240s |

#### Research Flow

1. **Query Analysis**: System analyzes your query
2. **Query Rewrite**: Vague queries are rewritten into search-ready variants
3. **Search**: Multiple search queries are generated
4. **Source Selection**: AI selects the most relevant sources
5. **Content Extraction**: Pages are scraped concurrently (code blocks preserved)
6. **Synthesis**: AI generates comprehensive response with citations

#### Code Block Support

For programming-related queries, the research results include properly formatted code examples with syntax highlighting. The scraper preserves `<pre>` and `<code>` HTML elements as markdown fenced code blocks throughout the pipeline.

#### Internationalized Messages

Research progress messages (in streaming mode) automatically adapt to the query language. Turkish queries produce Turkish status messages; English queries produce English messages.

#### Example Research Request

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tools/web-research" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "query": "Latest developments in AI coding agents",
    "max_sources": 5,
    "deep_mode": false
  }'
```

#### Streaming Research

For real-time progress updates:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tools/web-research/stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "query": "Latest AI news",
    "max_sources": 5
  }'
```

---

### Advanced Configuration

#### Rate Limiting

The API implements rate limiting to prevent abuse:

| Endpoint | Default Limit |
|----------|---------------|
| General | 60/min |
| Scrape | 60/min |
| Research | 15/min |

#### Caching

Research results are cached for 5 minutes (configurable) to improve response times.

#### Security Features

- **SSRF Protection**: Prevents requests to private networks
- **Input Validation**: All inputs are validated
- **API Key Authentication**: Optional API key protection
- **CORS**: Configurable cross-origin resource sharing

---

## Türkçe

### İçindekiler

1. [Başlangıç](#başlangıç)
2. [CLI Kullanımı](#cli-kullanımı)
3. [Web Arayüzü](#web-arayüzü)
4. [Araştırma Özellikleri](#araştırma-özellikleri)
5. [Gelişmiş Yapılandırma](#gelişmiş-yapılandırma)

---

### Başlangıç

[Kurulum](./installation-tr.md#kurulum-rehberi)'dan sonra uygulamayı üç şekilde kullanabilirsiniz:

1. **Web Arayüzü** (Yeni başlayanlar için önerilen)
2. **CLI** (Geliştiriciler ve otomasyon için)
3. **REST API** (Diğer sistemlerle entegrasyon için)

---

### CLI Kullanımı

#### Tek URL Kazıma

```bash
# Temel kullanım
web-scraper scrape https://ornek.com

# Dosyaya kaydet
web-scraper scrape https://ornek.com -o sonuc.json

# Pretty print JSON
web-scraper scrape https://ornek.com --pretty

# Özel timeout (saniye)
web-scraper scrape https://ornek.com --timeout 60

# Özel max bağlantı
web-scraper scrape https://ornek.com --max-links 200
```

#### API Sunucusunu Başlatma

```bash
# Varsayılan ayarlar
web-scraper serve

# Özel host ve port
web-scraper serve --host 0.0.0.0 --port 8080

# Geliştirme modu ile otomatik yeniden yükleme
web-scraper serve --reload

# Debug loglamayı etkinleştir
web-scraper serve --log-level DEBUG
```

#### Toplu Kazıma

```bash
# Birden fazla URL kazıma
web-scraper batch https://ornek.com https://ornek.org -o sonuclar.jsonl

# Özel eşzamanlılık (varsayılan: 5)
web-scraper batch url1 url2 url3 -o sonuclar.jsonl --concurrent 10
```

---

### Web Arayüzü

Web arayüzü, araştırma görevleri için modern bir UI sağlar.

#### Erişim

Tarayıcınızı açın ve gidin: **http://localhost:3000**

#### Özellikler

1. **Araştırma Sorgusu**: Araştırma sorunuzu girin
2. **Derin Mod**: Kapsamlı araştırma için toggle
3. **Ayarlar**:
   - AI Sağlayıcı seçimi (Ollama/OpenAI)
   - Model seçimi
   - API anahtarı yapılandırması
   - Tema (Açık/Koyu/Sistem)
   - Dil (İngilizce/Türkçe)

#### Ekran Görüntüsü

```
┌─────────────────────────────────────────────┐
│  🔍 Araştırma Sorgusu                      │
│  ┌─────────────────────────────────────────┐│
│  │ Yapay zeka son gelişmeler neler?      ││
│  └─────────────────────────────────────────┘│
│                                             │
│  [⚡ Derin Mod]  [🔍 Araştır]              │
│                                             │
│  Ayarlar (Tema | Dil | AI Sağlayıcı)       │
└─────────────────────────────────────────────┘
```

---

### Araştırma Özellikleri

#### Normal Mod vs Derin Mod

| Özellik | Normal Mod | Derin Mod |
|---------|------------|-----------|
| Max Kaynak | 5-15 | 15-50 |
| Kaynak Başına İçerik | 2,500 karakter | 8,000 karakter |
| Analiz Derinliği | Özet | Kapsamlı |
| Timeout | 120s | 240s |

#### Araştırma Akışı

1. **Sorgu Analizi**: Sistem sorgunuzu analiz eder
2. **Sorgu Yeniden Yazımı**: Belirsiz sorgular arama için optimize edilir
3. **Arama**: Birden fazla arama sorgusu oluşturulur
4. **Kaynak Seçimi**: AI en alakalı kaynakları seçer
5. **İçerik Çıkarma**: Sayfalar eşzamanlı olarak kazınır (kod blokları korunur)
6. **Sentez**: AI alıntılarla kapsamlı yanıt oluşturur

#### Kod Bloğu Desteği

Programlama ile ilgili sorgularda araştırma sonuçları söz dizimi vurgulaması ile düzgün biçimlendirilmiş kod örnekleri içerir. Kazıyıcı, `<pre>` ve `<code>` HTML öğelerini tüm boru hattı boyunca markdown kodlu bloklar olarak korur.

#### Uluslararasılaştırılmış Mesajlar

Araştırma ilerleme mesajları (yayın akışı modunda) sorgu diline göre otomatik olarak uyarlanır. Türkçe sorgular Türkçe durum mesajları üretir; İngilizce sorgular İngilizce mesajlar üretir.

#### Örnek Araştırma İsteği

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tools/web-research" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sizin-api-anahtariniz" \
  -d '{
    "query": "Yapay zeka kodlama ajanları son gelişmeler",
    "max_sources": 5,
    "deep_mode": false
  }'
```

#### Yayın Akışı Araştırması

Gerçek zamanlı ilerleme güncellemeleri için:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tools/web-research/stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sizin-api-anahtariniz" \
  -d '{
    "query": "Yapay zeka son haberler",
    "max_sources": 5
  }'
```

---

### Gelişmiş Yapılandırma

#### Hız Sınırlama

API, kötüye kullanımı önlemek için hız sınırlama uygular:

| Uç Nokta | Varsayılan Limit |
|----------|------------------|
| Genel | 60/dak |
| Kazıma | 60/dak |
| Araştırma | 15/dak |

#### Önbelleğe Alma

Yanıt sürelerini iyileştirmek için araştırma sonuçları 5 dakika önbelleğe alınır (yapılandırılabilir).

#### Güvenlik Özellikleri

- **SSRF Koruması**: Özel ağlara istekleri engeller
- **Giriş Doğrulama**: Tüm girdiler doğrulanır
- **API Anahtar Kimlik Doğrulama**: İsteğe bağlı API anahtarı koruması
- **CORS**: Yapılandırılabilir çapraz kaynak kaynak paylaşımı
