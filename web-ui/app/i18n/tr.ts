import type { Translations } from './en'

const tr: Translations = {
    heroTitle: 'Ay \u0131\u015f\u0131\u011f\u0131nda sohbet?',
    heroInputPlaceholder: 'Bug\u00fcn size nas\u0131l yard\u0131mc\u0131 olabilirim?',

    chatInputPlaceholder: 'Yan\u0131tla...',

    initiatingSearch: 'Arama ba\u015flat\u0131l\u0131yor...',
    researching: 'Ara\u015ft\u0131r\u0131l\u0131yor',
    researchCompleted: 'Ara\u015ft\u0131rma Tamamland\u0131',
    sourcesFound: '{count} kaynak bulundu',

    synthesizedIntro: 'Kapsaml\u0131 bir genel bak\u0131\u015f i\u00e7in son geli\u015fmeler sentezlendi',
    keyFindings: 'Temel Bulgular',
    recommendations: '\u00d6neriler',
    resultMode: 'Mod',
    resultModeDeep: 'Derin',
    resultModeStandard: 'Standart',
    resultAutoFocused: 'Otomatik odak',
    sourceCoverage: 'Kaynak Kapsamas\u0131',
    confidence: 'G\u00fcven',
    confidenceReason: 'G\u00fcven Gerek\u00e7esi',
    confidenceReasonUnavailable: 'Gerek\u00e7e belirtilmedi',
    showExtendedAnalysis: 'Geni\u015fletilmi\u015f analizi g\u00f6ster',
    dataTable: 'Veriler',
    dataMetric: 'Metrik',
    dataResult: 'Sonu\u00e7',
    dataSource: 'Kaynak',
    dataDate: 'Tarih',
    evidenceGateFailedTitle: 'Kan\u0131t kalitesi uyar\u0131s\u0131',
    evidenceGateFailedMessage: 'Otoriter/g\u00fcncel kan\u0131t yetersiz. Bu yan\u0131t\u0131 temkinli de\u011ferlendirin.',
    authorityMix: 'Otorite Da\u011f\u0131l\u0131m\u0131',
    retries: 'Tekrarlar',
    freshness: 'G\u00fcncellik',
    intentClass: 'Niyet',
    unknownValue: 'Bilinmiyor',

    deepMode: 'Derin Mod',
    options: 'Se\u00e7enekler',
    mode: 'Mod',

    aiDisclaimer: 'Yapay zeka hatal\u0131 bilgi \u00fcretebilir. L\u00fctfen \u00f6nemli iddialari do\u011frulay\u0131n.',

    settings: 'Ayarlar',
    aiProvider: 'Yapay Zeka Sa\u011flay\u0131c\u0131',
    localSelfHosted: 'Yerel / kendi sunucunuz',
    cloudApiKey: 'Bulut / API anahtar\u0131',
    model: 'Model',
    apiKey: 'API Anahtar\u0131',
    apiKeyPlaceholder: 'sk-...',
    apiKeyHelp: 'localStorage\'da saklan\u0131r. OpenAI isteklerini y\u00f6nlendirmek i\u00e7in sunucumuza g\u00f6nderilir.',
    fetchingModels: 'Modeller getiriliyor\u2026',
    modelsAvailable: '{count} model mevcut',
    networkError: 'A\u011f hatas\u0131 \u2014 varsay\u0131lanlar kullan\u0131l\u0131yor',
    noOllamaModels: 'Ollama modeli bulunamad\u0131 \u2014 Ollama \u00e7al\u0131\u015f\u0131yor mu?',
    saveAndClose: 'Kaydet ve Kapat',
    hideApiKey: 'API anahtar\u0131n\u0131 gizle',
    showApiKey: 'API anahtar\u0131n\u0131 g\u00f6ster',
    closeSettings: 'Ayarlar\u0131 kapat',

    theme: 'Tema',
    lightMode: 'A\u00e7\u0131k',
    darkMode: 'Koyu',
    systemMode: 'Sistem',

    language: 'Dil',

    searchAborted: 'Arama iptal edildi',
    researchError: 'Ara\u015ft\u0131rma s\u0131ras\u0131nda bir hata olu\u015ftu: {error}',
    connectionInterrupted: 'Ara\u015ft\u0131rma tamamlanamad\u0131. Ba\u011flant\u0131 kesilmi\u015f olabilir. L\u00fctfen tekrar deneyin.',
    copySelection: 'Se\u00e7imi kopyala',
    copied: 'Kopyaland\u0131!',

    // Research Status Messages
    researchStatusPreparingSearchQueries: 'Arama için sorgular hazırlanıyor...',
    researchStatusPlanningStrategy: 'Araştırma stratejisi planlanıyor...',
    researchStatusFoundSources: '{count} potansiyel kaynak bulundu. Derinlik: {depth}',
    researchStatusGatheringData: 'Kaynaklardan veriler toplanıyor...',
    researchStatusAnalyzingFindings: 'Bulgular analiz edilip sentezleniyor...',
    researchStatusGeneratingQueries: 'Daha iyi alma için {count} arama sorgusu varyantı oluşturuldu.',
    researchStatusAutoFocused: 'Bu tek niyetli sorgu i\u00e7in derin mod otomatik odakland\u0131 (daha iyi isabet).',

    researchProfile: 'Profil',
    profileAuto: 'Otomatik',
    profileTechnical: 'Teknik',
    profileNews: 'Haber',
    profileAcademic: 'Akademik',
    profileGeneral: 'Genel',

    ollamaBaseUrl: 'Ollama Sunucu URL\'si',
    ollamaBaseUrlPlaceholder: 'https://ollama.com  veya  http://localhost:11434',
    ollamaBaseUrlHelp: 'Genel model kütüphanesi için https://ollama.com kullanın. Kendi sunucunuz için özel URL girin.',
    ollamaApiKey: 'Ollama API Anahtarı',
    ollamaApiKeyPlaceholder: 'Bearer token (isteğe bağlı)',
    ollamaApiKeyHelp: 'Yalnızca kimlik doğrulamalı Ollama uç noktaları için gereklidir. localStorage\'da saklanır.',
}

export default tr
