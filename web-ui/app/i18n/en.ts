export interface Translations {
    // Hero / Landing
    heroTitle: string
    heroInputPlaceholder: string

    // Chat
    chatInputPlaceholder: string

    // Search / Loading
    initiatingSearch: string
    researching: string
    researchCompleted: string
    sourcesFound: string

    // Results
    synthesizedIntro: string
    keyFindings: string
    recommendations: string
    resultMode: string
    resultModeDeep: string
    resultModeStandard: string
    resultAutoFocused: string
    sourceCoverage: string
    confidence: string
    confidenceReason: string
    confidenceReasonUnavailable: string
    showExtendedAnalysis: string
    dataTable: string
    dataMetric: string
    dataResult: string
    dataSource: string
    dataDate: string
    evidenceGateFailedTitle: string
    evidenceGateFailedMessage: string
    authorityMix: string
    retries: string
    freshness: string
    intentClass: string
    unknownValue: string

    // Deep Mode
    deepMode: string
    options: string
    mode: string

    // Disclaimer
    aiDisclaimer: string

    // Settings
    settings: string
    aiProvider: string
    localSelfHosted: string
    cloudApiKey: string
    model: string
    apiKey: string
    apiKeyPlaceholder: string
    apiKeyHelp: string
    fetchingModels: string
    modelsAvailable: string
    networkError: string
    noOllamaModels: string
    saveAndClose: string
    hideApiKey: string
    showApiKey: string
    closeSettings: string

    // Theme
    theme: string
    lightMode: string
    darkMode: string
    systemMode: string

    // Language
    language: string

    // Errors
    searchAborted: string
    researchError: string
    connectionInterrupted: string
    copySelection: string
    copied: string

    // Research Status Messages
    researchStatusPreparingSearchQueries: string
    researchStatusPlanningStrategy: string
    researchStatusFoundSources: string
    researchStatusGatheringData: string
    researchStatusAnalyzingFindings: string
    researchStatusGeneratingQueries: string
    researchStatusAutoFocused: string

    // Research Profile
    researchProfile: string
    profileAuto: string
    profileTechnical: string
    profileNews: string
    profileAcademic: string
    profileGeneral: string

    // Ollama settings
    ollamaBaseUrl: string
    ollamaBaseUrlPlaceholder: string
    ollamaBaseUrlHelp: string
    ollamaApiKey: string
    ollamaApiKeyPlaceholder: string
    ollamaApiKeyHelp: string
}

const en: Translations = {
    heroTitle: 'Moonlit chat?',
    heroInputPlaceholder: 'How can I help you today?',

    chatInputPlaceholder: 'Reply...',

    initiatingSearch: 'Initiating search...',
    researching: 'Researching',
    researchCompleted: 'Research Completed',
    sourcesFound: '{count} sources found',

    synthesizedIntro: 'Synthesized breaking developments for comprehensive overview',
    keyFindings: 'Key Findings',
    recommendations: 'Recommendations',
    resultMode: 'Mode',
    resultModeDeep: 'Deep',
    resultModeStandard: 'Standard',
    resultAutoFocused: 'Auto-focused',
    sourceCoverage: 'Source Coverage',
    confidence: 'Confidence',
    confidenceReason: 'Confidence Reason',
    confidenceReasonUnavailable: 'No reason provided',
    showExtendedAnalysis: 'Show extended analysis',
    dataTable: 'Data',
    dataMetric: 'Metric',
    dataResult: 'Result',
    dataSource: 'Source',
    dataDate: 'Date',
    evidenceGateFailedTitle: 'Evidence quality warning',
    evidenceGateFailedMessage: 'Authoritative/fresh evidence is insufficient. Treat this answer as tentative.',
    authorityMix: 'Authority Mix',
    retries: 'Retries',
    freshness: 'Freshness',
    intentClass: 'Intent',
    unknownValue: 'Unknown',

    deepMode: 'Deep Mode',
    options: 'Options',
    mode: 'Mode',

    aiDisclaimer: 'AI may produce inaccurate information. Please verify important claims.',

    settings: 'Settings',
    aiProvider: 'AI Provider',
    localSelfHosted: 'Local / self-hosted',
    cloudApiKey: 'Cloud / API key',
    model: 'Model',
    apiKey: 'API Key',
    apiKeyPlaceholder: 'sk-...',
    apiKeyHelp: 'Stored in localStorage. Sent to our server to proxy requests to OpenAI.',
    fetchingModels: 'Fetching models\u2026',
    modelsAvailable: '{count} models available',
    networkError: 'Network error \u2014 using defaults',
    noOllamaModels: 'No Ollama models found \u2014 is Ollama running?',
    saveAndClose: 'Save & Close',
    hideApiKey: 'Hide API key',
    showApiKey: 'Show API key',
    closeSettings: 'Close settings',

    theme: 'Theme',
    lightMode: 'Light',
    darkMode: 'Dark',
    systemMode: 'System',

    language: 'Language',

    searchAborted: 'Search aborted',
    researchError: 'I encountered an error while researching: {error}',
    connectionInterrupted: 'Research did not complete. The connection may have been interrupted. Please try again.',
    copySelection: 'Copy selection',
    copied: 'Copied!',

    // Research Status Messages
    researchStatusPreparingSearchQueries: 'Preparing search queries...',
    researchStatusPlanningStrategy: 'Planning research strategy...',
    researchStatusFoundSources: '{count} potential sources found. Depth: {depth}',
    researchStatusGatheringData: 'Gathering data from sources...',
    researchStatusAnalyzingFindings: 'Analyzing and synthesizing findings...',
    researchStatusGeneratingQueries: 'Generated {count} search query variants for better retrieval.',
    researchStatusAutoFocused: 'Deep mode auto-focused for this single-intent query (improved precision).',

    researchProfile: 'Profile',
    profileAuto: 'Auto',
    profileTechnical: 'Technical',
    profileNews: 'News',
    profileAcademic: 'Academic',
    profileGeneral: 'General',

    ollamaBaseUrl: 'Ollama Host URL',
    ollamaBaseUrlPlaceholder: 'https://ollama.com  or  http://localhost:11434',
    ollamaBaseUrlHelp: 'Use https://ollama.com to browse the public model library. Set a custom URL for your self-hosted or cloud Ollama instance.',
    ollamaApiKey: 'Ollama API Key',
    ollamaApiKeyPlaceholder: 'Bearer token (optional)',
    ollamaApiKeyHelp: 'Required only for authenticated Ollama endpoints. Stored in localStorage.',
}

export default en
