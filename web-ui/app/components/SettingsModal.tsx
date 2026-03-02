'use client'

import { useEffect, useRef, useState } from 'react'
import { X, Eye, EyeOff, Loader2, Sun, Moon, Monitor } from 'lucide-react'
import { useTheme } from '../contexts/ThemeProvider'
import { useLanguage } from '../contexts/LanguageProvider'

const OPENAI_MODELS_FALLBACK = [
    'gpt-4o',
    'gpt-4o-mini',
    'gpt-4-turbo',
    'o1',
    'o1-mini',
    'o3-mini',
]

interface SettingsModalProps {
    open: boolean
    onClose: () => void
    provider: 'ollama' | 'openai'
    setProvider: (p: 'ollama' | 'openai') => void
    selectedModel: string
    setSelectedModel: (m: string) => void
    openaiModel: string
    setOpenaiModel: (m: string) => void
    openaiApiKey: string
    setOpenaiApiKey: (k: string) => void
    ollamaModels: string[]
    ollamaBaseUrl: string
    setOllamaBaseUrl: (url: string) => void
    ollamaApiKey: string
    setOllamaApiKey: (k: string) => void
}

export default function SettingsModal({
    open,
    onClose,
    provider,
    setProvider,
    selectedModel,
    setSelectedModel,
    openaiModel,
    setOpenaiModel,
    openaiApiKey,
    setOpenaiApiKey,
    ollamaModels,
    ollamaBaseUrl,
    setOllamaBaseUrl,
    ollamaApiKey,
    setOllamaApiKey,
}: SettingsModalProps) {
    const [showKey, setShowKey] = useState(false)
    const [showOllamaKey, setShowOllamaKey] = useState(false)
    const [openaiModels, setOpenaiModels] = useState<string[]>(OPENAI_MODELS_FALLBACK)
    const [fetchingModels, setFetchingModels] = useState(false)
    const [fetchError, setFetchError] = useState<string | null>(null)

    const { theme, setTheme } = useTheme()
    const { language, setLanguage, t } = useLanguage()

    const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
    const openaiModelRef = useRef(openaiModel)
    useEffect(() => { openaiModelRef.current = openaiModel }, [openaiModel])

    // Fetch real model list from OpenAI when key looks valid
    useEffect(() => {
        if (debounceRef.current) clearTimeout(debounceRef.current)

        const keyLooksValid =
            openaiApiKey.length > 20 &&
            (openaiApiKey.startsWith('sk-') || openaiApiKey.startsWith('sk-proj-'))

        if (!keyLooksValid) {
            setOpenaiModels(OPENAI_MODELS_FALLBACK)
            setFetchError(null)
            return
        }

        debounceRef.current = setTimeout(async () => {
            setFetchingModels(true)
            setFetchError(null)
            try {
                const res = await fetch('/api/openai/models', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ apiKey: openaiApiKey }),
                })
                const data = await res.json() as { models?: string[]; error?: string }
                if (!res.ok || data.error) {
                    setFetchError(data.error ?? 'Could not fetch models')
                    setOpenaiModels(OPENAI_MODELS_FALLBACK)
                } else {
                    const models = data.models ?? OPENAI_MODELS_FALLBACK
                    setOpenaiModels(models)
                    if (!models.includes(openaiModelRef.current) && models.length > 0) {
                        setOpenaiModel(models[0])
                    }
                }
            } catch {
                setFetchError(t.networkError)
                setOpenaiModels(OPENAI_MODELS_FALLBACK)
            } finally {
                setFetchingModels(false)
            }
        }, 600)

        return () => {
            if (debounceRef.current) clearTimeout(debounceRef.current)
        }
    }, [openaiApiKey, setOpenaiModel, t.networkError])

    if (!open) return null

    const sectionLabelStyle: React.CSSProperties = {
        color: 'var(--text-faint)',
    }

    const cardStyle = (active: boolean): React.CSSProperties => ({
        borderWidth: '1px',
        borderStyle: 'solid',
        borderColor: active ? 'var(--accent-border-active)' : 'var(--border)',
        backgroundColor: active ? 'var(--accent-bg)' : 'transparent',
        color: active ? 'var(--text-primary)' : 'var(--text-muted)',
    })

    const inputStyle: React.CSSProperties = {
        backgroundColor: 'var(--bg-primary)',
        borderWidth: '1px',
        borderStyle: 'solid',
        borderColor: 'var(--border)',
        color: 'var(--text-primary)',
    }

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center backdrop-blur-sm"
            style={{ backgroundColor: 'rgba(0, 0, 0, 0.6)' }}
            onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
        >
            <div
                className="relative w-full max-w-md mx-4 rounded-2xl shadow-2xl p-6 max-h-[85vh] overflow-y-auto custom-scrollbar"
                style={{
                    backgroundColor: 'var(--bg-elevated)',
                    borderWidth: '1px',
                    borderStyle: 'solid',
                    borderColor: 'var(--border)',
                    color: 'var(--text-primary)',
                }}
            >
                {/* Close button */}
                <button
                    onClick={onClose}
                    className="absolute top-4 right-4 transition-colors"
                    style={{ color: 'var(--text-faint)' }}
                    onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--text-secondary)' }}
                    onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-faint)' }}
                    aria-label={t.closeSettings}
                >
                    <X className="w-4 h-4" />
                </button>

                <h2
                    className="text-sm font-bold tracking-widest uppercase mb-6"
                    style={{ color: 'var(--text-muted)' }}
                >
                    {t.settings}
                </h2>

                {/* ── Theme selector ── */}
                <div className="mb-6">
                    <p className="text-xs uppercase tracking-widest mb-3" style={sectionLabelStyle}>
                        {t.theme}
                    </p>
                    <div className="grid grid-cols-3 gap-2">
                        {([
                            { key: 'light' as const, label: t.lightMode, icon: Sun },
                            { key: 'dark' as const, label: t.darkMode, icon: Moon },
                            { key: 'system' as const, label: t.systemMode, icon: Monitor },
                        ]).map(({ key, label, icon: Icon }) => (
                            <button
                                key={key}
                                type="button"
                                onClick={() => setTheme(key)}
                                className="flex flex-col items-center p-3 rounded-xl text-center transition-all"
                                style={cardStyle(theme === key)}
                            >
                                <Icon className="w-4 h-4 mb-1.5" style={{ opacity: theme === key ? 1 : 0.5 }} />
                                <span className="text-xs font-semibold">{label}</span>
                            </button>
                        ))}
                    </div>
                </div>

                {/* ── Language selector ── */}
                <div className="mb-6">
                    <p className="text-xs uppercase tracking-widest mb-3" style={sectionLabelStyle}>
                        {t.language}
                    </p>
                    <div className="grid grid-cols-2 gap-2">
                        {([
                            { key: 'en' as const, label: 'English', flag: '🇬🇧' },
                            { key: 'tr' as const, label: 'Türkçe', flag: '🇹🇷' },
                        ]).map(({ key, label, flag }) => (
                            <button
                                key={key}
                                type="button"
                                onClick={() => setLanguage(key)}
                                className="flex items-center gap-2 p-3 rounded-xl text-left transition-all"
                                style={cardStyle(language === key)}
                            >
                                <span className="text-base">{flag}</span>
                                <span className="text-xs font-semibold">{label}</span>
                            </button>
                        ))}
                    </div>
                </div>

                {/* ── Provider selector ── */}
                <div className="mb-6">
                    <p className="text-xs uppercase tracking-widest mb-3" style={sectionLabelStyle}>
                        {t.aiProvider}
                    </p>
                    <div className="grid grid-cols-2 gap-2">
                        <button
                            type="button"
                            onClick={() => setProvider('ollama')}
                            className="flex flex-col items-start p-3 rounded-xl text-left transition-all"
                            style={cardStyle(provider === 'ollama')}
                        >
                            <span className="text-base mb-1">🔗</span>
                            <span className="text-xs font-semibold">Ollama</span>
                            <span className="text-[0.65rem] mt-0.5" style={{ opacity: 0.5 }}>{t.localSelfHosted}</span>
                        </button>
                        <button
                            type="button"
                            onClick={() => setProvider('openai')}
                            className="flex flex-col items-start p-3 rounded-xl text-left transition-all"
                            style={cardStyle(provider === 'openai')}
                        >
                            <span className="text-base mb-1">✦</span>
                            <span className="text-xs font-semibold">OpenAI</span>
                            <span className="text-[0.65rem] mt-0.5" style={{ opacity: 0.5 }}>{t.cloudApiKey}</span>
                        </button>
                    </div>
                </div>

                {/* ── Ollama settings ── */}
                {provider === 'ollama' && (
                    <div className="space-y-4">
                        {/* Model */}
                        <div>
                            <label
                                className="block text-xs uppercase tracking-widest mb-2"
                                style={sectionLabelStyle}
                            >
                                {t.model}
                            </label>
                            {ollamaModels.length > 0 ? (
                                <select
                                    value={selectedModel}
                                    onChange={(e) => setSelectedModel(e.target.value)}
                                    className="w-full rounded-lg px-3 py-2 text-sm focus:outline-none appearance-none"
                                    style={{
                                        ...inputStyle,
                                    }}
                                >
                                    {ollamaModels.map((m) => (
                                        <option key={m} value={m}>{m}</option>
                                    ))}
                                </select>
                            ) : (
                                <p className="text-xs italic" style={{ color: 'var(--text-ghost)' }}>
                                    {t.noOllamaModels}
                                </p>
                            )}
                        </div>

                        {/* Ollama Host URL */}
                        <div>
                            <label
                                className="block text-xs uppercase tracking-widest mb-2"
                                style={sectionLabelStyle}
                            >
                                {t.ollamaBaseUrl}
                            </label>
                            <input
                                type="text"
                                value={ollamaBaseUrl}
                                onChange={(e) => setOllamaBaseUrl(e.target.value)}
                                placeholder={t.ollamaBaseUrlPlaceholder}
                                className="w-full rounded-lg px-3 py-2 text-sm focus:outline-none"
                                style={inputStyle}
                            />
                            <p className="text-[0.65rem] mt-1.5" style={{ color: 'var(--text-placeholder)' }}>
                                {t.ollamaBaseUrlHelp}
                            </p>
                        </div>

                        {/* Ollama API Key */}
                        <div>
                            <label
                                className="block text-xs uppercase tracking-widest mb-2"
                                style={sectionLabelStyle}
                            >
                                {t.ollamaApiKey}
                            </label>
                            <div className="relative">
                                <input
                                    type={showOllamaKey ? 'text' : 'password'}
                                    value={ollamaApiKey}
                                    onChange={(e) => setOllamaApiKey(e.target.value)}
                                    placeholder={t.ollamaApiKeyPlaceholder}
                                    className="w-full rounded-lg px-3 py-2 pr-10 text-sm focus:outline-none"
                                    style={inputStyle}
                                />
                                <button
                                    type="button"
                                    onClick={() => setShowOllamaKey(!showOllamaKey)}
                                    className="absolute right-3 top-1/2 -translate-y-1/2 transition-colors"
                                    style={{ color: 'var(--text-ghost)' }}
                                    onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--text-muted)' }}
                                    onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-ghost)' }}
                                    aria-label={showOllamaKey ? t.hideApiKey : t.showApiKey}
                                >
                                    {showOllamaKey ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                                </button>
                            </div>
                            <p className="text-[0.65rem] mt-1.5" style={{ color: 'var(--text-placeholder)' }}>
                                {t.ollamaApiKeyHelp}
                            </p>
                        </div>
                    </div>
                )}

                {/* ── OpenAI settings ── */}
                {provider === 'openai' && (
                    <div className="space-y-4">
                        {/* API Key */}
                        <div>
                            <label
                                className="block text-xs uppercase tracking-widest mb-2"
                                style={sectionLabelStyle}
                            >
                                {t.apiKey}
                            </label>
                            <div className="relative">
                                <input
                                    type={showKey ? 'text' : 'password'}
                                    value={openaiApiKey}
                                    onChange={(e) => setOpenaiApiKey(e.target.value)}
                                    placeholder={t.apiKeyPlaceholder}
                                    className="w-full rounded-lg px-3 py-2 pr-10 text-sm focus:outline-none"
                                    style={inputStyle}
                                />
                                <button
                                    type="button"
                                    onClick={() => setShowKey(!showKey)}
                                    className="absolute right-3 top-1/2 -translate-y-1/2 transition-colors"
                                    style={{ color: 'var(--text-ghost)' }}
                                    onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--text-muted)' }}
                                    onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-ghost)' }}
                                    aria-label={showKey ? t.hideApiKey : t.showApiKey}
                                >
                                    {showKey ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                                </button>
                            </div>
                            <p className="text-[0.65rem] mt-1.5" style={{ color: 'var(--text-placeholder)' }}>
                                {t.apiKeyHelp}
                            </p>
                        </div>

                        {/* Model */}
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <label
                                    className="block text-xs uppercase tracking-widest"
                                    style={sectionLabelStyle}
                                >
                                    {t.model}
                                </label>
                                {fetchingModels && (
                                    <span className="flex items-center gap-1 text-[0.65rem]" style={{ color: 'var(--accent-muted)' }}>
                                        <Loader2 className="w-2.5 h-2.5 animate-spin" />
                                        {t.fetchingModels}
                                    </span>
                                )}
                                {!fetchingModels && fetchError && (
                                    <span className="text-[0.65rem]" style={{ color: 'var(--error-text)' }}>{fetchError}</span>
                                )}
                                {!fetchingModels && !fetchError && openaiModels.length > OPENAI_MODELS_FALLBACK.length && (
                                    <span className="text-[0.65rem]" style={{ color: 'var(--accent-muted)' }}>
                                        {t.modelsAvailable.replace('{count}', String(openaiModels.length))}
                                    </span>
                                )}
                            </div>
                            <select
                                value={openaiModel}
                                onChange={(e) => setOpenaiModel(e.target.value)}
                                disabled={fetchingModels}
                                className="w-full rounded-lg px-3 py-2 text-sm focus:outline-none appearance-none disabled:opacity-40"
                                style={inputStyle}
                            >
                                {openaiModels.map((m) => (
                                    <option key={m} value={m}>{m}</option>
                                ))}
                            </select>
                        </div>
                    </div>
                )}

                {/* Save / close */}
                <button
                    type="button"
                    onClick={onClose}
                    className="mt-6 w-full py-2.5 rounded-xl text-sm font-semibold transition-colors"
                    style={{
                        backgroundColor: 'var(--accent-bg)',
                        borderWidth: '1px',
                        borderStyle: 'solid',
                        borderColor: 'var(--accent-border)',
                        color: 'var(--text-secondary)',
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = 'var(--accent-bg-hover)' }}
                    onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = 'var(--accent-bg)' }}
                >
                    {t.saveAndClose}
                </button>
            </div>
        </div>
    )
}
