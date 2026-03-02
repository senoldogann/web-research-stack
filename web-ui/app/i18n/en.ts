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

    // Deep Mode
    deepMode: string

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

    // Research Profile
    researchProfile: string
    profileTechnical: string
    profileNews: string
    profileAcademic: string

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

    deepMode: 'Deep Mode',

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

    researchProfile: 'Profile',
    profileTechnical: 'Technical',
    profileNews: 'News',
    profileAcademic: 'Academic',

    ollamaBaseUrl: 'Ollama Host URL',
    ollamaBaseUrlPlaceholder: 'http://localhost:11434',
    ollamaBaseUrlHelp: 'Leave empty for local Ollama. Set to a cloud/remote Ollama URL (e.g. https://my-ollama.example.com).',
    ollamaApiKey: 'Ollama API Key',
    ollamaApiKeyPlaceholder: 'Bearer token (optional)',
    ollamaApiKeyHelp: 'Required only for authenticated Ollama endpoints. Stored in localStorage.',
}

export default en
