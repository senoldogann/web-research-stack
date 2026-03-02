# Contributing Guide / Katkıda Bulunma Rehberi

[English](#english) | [Türkçe](#türkçe)

---

## English

### Welcome to Web Scraper!

Thank you for considering contributing to Web Scraper. This document provides guidelines for contributing to this project.

### Code of Conduct

By participating in this project, you agree to follow our Code of Conduct:

- **Be respectful** - Treat all contributors with respect
- **Be inclusive** - Welcome everyone regardless of background
- **Be constructive** - Provide helpful feedback
- **Be patient** - Remember that everyone is learning

### How to Contribute

#### 1. Fork the Repository

```bash
git clone https://github.com/senoldogann/web-research-stack.git
cd web-research-stack
```

#### 2. Create a Feature Branch

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/bug-description
```

#### 3. Set Up Development Environment

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Install frontend dependencies
cd web-ui
npm install
```

#### 4. Make Your Changes

- Follow the existing code style
- Write meaningful commit messages
- Add tests for new features
- Update documentation as needed

#### 5. Run Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=web_scraper

# Run specific test file
pytest tests/test_api.py
```

#### 6. Run Linting

```bash
# Python linting
ruff check .

# JavaScript/TypeScript linting
cd web-ui && npm run lint
```

#### 7. Submit a Pull Request

1. Push your changes to your fork:
```bash
git push origin feature/your-feature-name
```

2. Open a Pull Request on GitHub

3. Fill in the PR template with:
   - Description of changes
   - Related issues
   - Test results

### Coding Standards

#### Python

- Follow [PEP 8](https://www.python.org/dev/peps/pep-0008/)
- Use type hints where possible
- Write docstrings for all public functions
- Maximum line length: 100 characters

#### JavaScript/TypeScript

- Follow ESLint configuration
- Use meaningful variable names
- Write comments in English
- Prefer functional components in React

### Project Structure

```
web-scraper/
├── web_scraper/          # Python backend
│   ├── api.py            # FastAPI endpoints
│   ├── scrapers.py       # Core scraping logic
│   ├── async_scrapers.py # Async scraping
│   ├── config.py         # Configuration
│   └── research_agent.py # AI research
├── web-ui/               # React frontend
│   ├── app/              # Next.js app router
│   ├── components/       # React components
│   └── contexts/         # React contexts
├── tests/                # Python tests
├── docs/                 # Documentation
└── scripts/              # Utility scripts
```

### Commit Message Guidelines

Format:
```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation
- `style`: Code style
- `refactor`: Code refactoring
- `test`: Tests
- `chore`: Maintenance

Example:
```
feat(research): Add deep mode for comprehensive research

- Added deep_mode parameter
- Increased source limits
- Updated synthesis prompts

Closes #123
```

### Reporting Issues

When reporting issues, please include:

1. **Title**: Clear, descriptive title
2. **Description**: Detailed explanation
3. **Steps to Reproduce**: How to reproduce the issue
4. **Expected Behavior**: What you expected
5. **Actual Behavior**: What actually happened
6. **Environment**: OS, Python version, etc.

### Communication

- **GitHub Issues**: For bug reports and feature requests
- **Discussions**: For questions and general discussion

---

## Türkçe

### Web Scraper'a Hoş Geldiniz!

Web Scraper'a katkıda bulunmayı düşündüğünüz için teşekkür ederiz. Bu belge, bu projeye katkıda bulunmak için rehberlik sağlar.

### Davranış Kuralları

Bu projeye katılarak Davranış Kurallarımızı takip etmeyi kabul edersiniz:

- **Saygılı olun** - Tüm katkıcılara saygıyla davranın
- **Kapsayıcı olun** - Herkesi arka planından bağımsız olarak karşılayın
- **Yapıcı olun** - Yararlı geri bildirim sağlayın
- **Sabırlı olun** - Herkesin öğrendiğini unutmayın

### Nasıl Katkıda Bulunulur

#### 1. Depoyu Fork Edin

```bash
git clone https://github.com/senoldogann/web-research-stack.git
cd web-research-stack
```

#### 2. Özellik Dalı Oluşturun

```bash
git checkout -b ozellik/sizin-ozellik-adiniz
# veya
git checkout -b duzeltme/hata-aciklamasi
```

#### 3. Geliştirme Ortamını Kurun

```bash
# Sanal ortam oluştur
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Bağımlılıkları yükle
pip install -e ".[dev]"

# Frontend bağımlılıklarını yükle
cd web-ui
npm install
```

#### 4. Değişikliklerinizi Yapın

- Mevcut kod stilini takip edin
- Anlamlı commit mesajları yazın
- Yeni özellikler için testler ekleyin
- Gerekirse dokümantasyonu güncelleyin

#### 5. Testleri Çalıştırın

```bash
# Tüm testleri çalıştır
pytest

# Coverage ile çalıştır
pytest --cov=web_scraper

# Belirli test dosyasını çalıştır
pytest tests/test_api.py
```

#### 6. Linting'i Çalıştırın

```bash
# Python linting
ruff check .

# JavaScript/TypeScript linting
cd web-ui && npm run lint
```

#### 7. Pull Request Gönderin

1. Değişikliklerinizi fork'unuza gönderin:
```bash
git push origin ozellik/sizin-ozellik-adiniz
```

2. GitHub'da Pull Request açın

3. PR şablonunu doldurun:
   - Değişikliklerin açıklaması
   - İlgili sorunlar
   - Test sonuçları

### Kodlama Standartları

#### Python

- [PEP 8](https://www.python.org/dev/peps/pep-0008/)'i takip edin
- Mümkün olduğunda tip ipuçları kullanın
- Tüm genel fonksiyonlar için docstrings yazın
- Maksimum satır uzunluğu: 100 karakter

#### JavaScript/TypeScript

- ESLint yapılandırmasını takip edin
- Anlamlı değişken isimleri kullanın
- Yorumları İngilizce yazın
- React'te fonksiyonel componentleri tercih edin

### Proje Yapısı

```
web-scraper/
├── web_scraper/          # Python backend
│   ├── api.py            # FastAPI endpoints
│   ├── scrapers.py       # Çekirdek kazıma mantığı
│   ├── async_scrapers.py # Async kazıma
│   ├── config.py         # Yapılandırma
│   └── research_agent.py # AI araştırması
├── web-ui/               # React frontend
│   ├── app/              # Next.js app router
│   ├── components/       # React componentleri
│   └── contexts/         # React contexts
├── tests/                # Python testleri
├── docs/                 # Dokümantasyon
└── scripts/              # Yardımcı scriptler
```

### Commit Mesajı Kuralları

Format:
```
<tip>(<kapsam>): <açıklama>

[isteğe bağlı gövde]

[isteğe bağlı footer]
```

Tipler:
- `feat`: Yeni özellik
- `fix`: Hata düzeltme
- `docs`: Dokümantasyon
- `style`: Kod stili
- `refactor`: Kod refactoring
- `test`: Testler
- `chore`: Bakım

Örnek:
```
feat(arastirma): Kapsamli arastirma icin derin mod eklendi

- derin_mod parametresi eklendi
- kaynak sinirlari artirildi
- sentez istemleri guncellendi

Kapatir #123
```

### Sorun Bildirme

Sorun bildirirken lütfen şunları ekleyin:

1. **Başlık**: Açık, tanımlayıcı başlık
2. **Açıklama**: Ayrıntılı açıklama
3. **Yeniden Oluşturma Adımları**: Sorunu nasıl yeniden oluşturabilirsiniz?
4. **Beklenen Davranış**: Ne bekliyordunuz?
5. **Gerçek Davranış**: Ne oldu?
6. **Ortam**: İşletim sistemi, Python sürümü vb.

### İletişim

- **GitHub Issues**: Hata raporları ve özellik istekleri için
- **Tartışmalar**: Sorular ve genel tartışmalar için
