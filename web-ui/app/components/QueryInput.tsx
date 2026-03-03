'use client'

import React, { useRef, useState, useEffect } from 'react'
import { ArrowUpRight, Sparkles, Square, Settings, Plus } from 'lucide-react'
import { useLanguage } from '../contexts/LanguageProvider'

// ── Inline SVG icons for research profiles ──────────────────────────────────

function IconTechnical({ className }: { className?: string }) {
    return (
        <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="4 5 1 8 4 11" />
            <polyline points="12 5 15 8 12 11" />
            <line x1="9.5" y1="3" x2="6.5" y2="13" />
        </svg>
    )
}

function IconAuto({ className }: { className?: string }) {
    return (
        <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="8" cy="8" r="6" />
            <path d="M5.5 10.5 L8 5 L10.5 10.5" />
            <line x1="6.5" y1="8.8" x2="9.5" y2="8.8" />
        </svg>
    )
}

function IconNews({ className }: { className?: string }) {
    return (
        <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="1" y="3" width="14" height="11" rx="1.5" />
            <line x1="1" y1="6.5" x2="7.5" y2="6.5" />
            <line x1="1" y1="9" x2="7.5" y2="9" />
            <line x1="1" y1="11.5" x2="7.5" y2="11.5" />
            <rect x="9" y="6.5" width="4.5" height="5" rx="0.5" />
        </svg>
    )
}

function IconAcademic({ className }: { className?: string }) {
    return (
        <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="8 2 15 5.5 8 9 1 5.5" />
            <path d="M4 7.5v4c0 1.1 1.8 2 4 2s4-.9 4-2v-4" />
            <line x1="15" y1="5.5" x2="15" y2="9.5" />
        </svg>
    )
}

// ── Component ────────────────────────────────────────────────────────────────

interface QueryInputProps {
    variant: 'hero' | 'chat'
    input: string
    setInput: (val: string) => void
    isLoading: boolean
    deepMode: boolean
    setDeepMode: (val: boolean) => void
    researchProfile: 'technical' | 'news' | 'academic' | 'auto'
    setResearchProfile: (val: 'technical' | 'news' | 'academic' | 'auto') => void
    provider: string
    openaiModel: string
    selectedModel: string
    onSubmit: (e: React.FormEvent) => void
    onStop: () => void
    onKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void
    onOpenSettings: () => void
}

export default function QueryInput({
    variant,
    input,
    setInput,
    isLoading,
    deepMode,
    setDeepMode,
    researchProfile,
    setResearchProfile,
    provider,
    openaiModel,
    selectedModel,
    onSubmit,
    onStop,
    onKeyDown,
    onOpenSettings,
}: QueryInputProps) {
    const { t } = useLanguage()
    const isHero = variant === 'hero'
    const [plusOpen, setPlusOpen] = useState(false)
    const dropdownRef = useRef<HTMLDivElement>(null)

    // Close dropdown on outside click
    useEffect(() => {
        if (!plusOpen) return
        const handler = (e: MouseEvent) => {
            if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
                setPlusOpen(false)
            }
        }
        document.addEventListener('mousedown', handler)
        return () => document.removeEventListener('mousedown', handler)
    }, [plusOpen])

    const profiles: {
        value: 'technical' | 'news' | 'academic' | 'auto'
        label: string
        Icon: ({ className }: { className?: string }) => React.ReactElement
    }[] = [
        { value: 'auto',      label: t.profileAuto ?? 'Auto',         Icon: IconAuto },
        { value: 'technical', label: t.profileTechnical, Icon: IconTechnical },
        { value: 'news',      label: t.profileNews,      Icon: IconNews },
        { value: 'academic',  label: t.profileAcademic,  Icon: IconAcademic },
    ]

    const activeProfile = profiles.find((p) => p.value === researchProfile)!

    return (
        <form
            onSubmit={onSubmit}
            className={`rounded-2xl transition-all ${isHero ? '' : 'shadow-2xl'}`}
            style={{
                backgroundColor: 'var(--bg-secondary)',
                borderWidth: '1px',
                borderStyle: 'solid',
                borderColor: isHero ? 'var(--border)' : 'var(--border-muted)',
            }}
        >
            <div className="flex items-end pt-1 px-1">
                <textarea
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={onKeyDown}
                    placeholder={isHero ? t.heroInputPlaceholder : t.chatInputPlaceholder}
                    rows={isHero ? 2 : 1}
                    style={isHero ? undefined : { height: 'auto' }}
                    className={`${
                        isHero
                            ? 'w-full text-[1.05rem]'
                            : 'flex-1 text-[1rem] min-h-[52px] max-h-[160px]'
                    } bg-transparent resize-none outline-none py-4 px-4`}
                />
                <div className="flex items-center gap-2 mb-2 mr-2">
                    {isLoading ? (
                        <button
                            type="button"
                            onClick={onStop}
                            className="p-2 rounded-xl transition-all"
                            style={{
                                backgroundColor: 'var(--surface-overlay)',
                                color: 'var(--text-primary)',
                            }}
                            onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = 'var(--surface-overlay-hover)' }}
                            onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = 'var(--surface-overlay)' }}
                        >
                            <Square className="w-5 h-5 fill-current" />
                        </button>
                    ) : input.trim() ? (
                        <button
                            type="submit"
                            className="p-2 rounded-xl transition-all"
                            style={{
                                backgroundColor: 'var(--accent)',
                                color: 'var(--bg-primary)',
                            }}
                            onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.8' }}
                            onMouseLeave={(e) => { e.currentTarget.style.opacity = '1' }}
                        >
                            <ArrowUpRight className="w-5 h-5" />
                        </button>
                    ) : null}
                </div>
            </div>

            {/* Bottom toolbar */}
            <div className="flex items-center justify-between px-3 pb-2.5 gap-2">
                <div className="flex items-center gap-2 flex-wrap">

                    {/* + button — opens dropdown */}
                    <div className="relative" ref={dropdownRef}>
                        <button
                            type="button"
                            onClick={() => setPlusOpen((o) => !o)}
                            className="flex items-center gap-1 pl-1.5 pr-2 py-1 rounded-lg text-xs font-semibold transition-all"
                            style={plusOpen || deepMode ? {
                                backgroundColor: 'var(--accent-bg)',
                                color: 'var(--accent)',
                                borderWidth: '1px', borderStyle: 'solid', borderColor: 'var(--accent-border)',
                            } : {
                                backgroundColor: 'var(--surface-hover)',
                                color: 'var(--text-muted)',
                                borderWidth: '1px', borderStyle: 'solid', borderColor: 'var(--border-subtle)',
                            }}
                            title="Options"
                        >
                            <Plus className="w-3 h-3" />
                            {/* Show active profile icon + deep badge as hints */}
                            <activeProfile.Icon className="w-3 h-3 opacity-70" />
                            {deepMode && <Sparkles className="w-3 h-3" />}
                        </button>

                        {/* Dropdown panel */}
                        {plusOpen && (
                            <div
                                className="absolute bottom-full left-0 mb-2 rounded-xl shadow-xl overflow-hidden z-50"
                                style={{
                                    minWidth: '200px',
                                    backgroundColor: 'var(--bg-secondary)',
                                    border: '1px solid var(--border)',
                                }}
                            >
                                {/* Deep Mode row */}
                                <div className="px-2 pt-2 pb-1">
                                    <p className="text-[0.6rem] font-semibold uppercase tracking-widest px-1 pb-1" style={{ color: 'var(--text-faint)' }}>
                                        Mode
                                    </p>
                                    <button
                                        type="button"
                                        onClick={() => { setDeepMode(!deepMode) }}
                                        className="w-full flex items-center justify-between px-2.5 py-2 rounded-lg text-xs font-semibold transition-all"
                                        style={deepMode ? {
                                            backgroundColor: 'var(--accent-bg)',
                                            color: 'var(--accent)',
                                        } : {
                                            backgroundColor: 'transparent',
                                            color: 'var(--text-secondary)',
                                        }}
                                        onMouseEnter={(e) => { if (!deepMode) e.currentTarget.style.backgroundColor = 'var(--surface-hover)' }}
                                        onMouseLeave={(e) => { if (!deepMode) e.currentTarget.style.backgroundColor = 'transparent' }}
                                    >
                                        <span className="flex items-center gap-2">
                                            <Sparkles className="w-3.5 h-3.5" />
                                            {t.deepMode}
                                        </span>
                                        {/* Toggle pill */}
                                        <span
                                            className="w-7 h-4 rounded-full flex items-center transition-all shrink-0"
                                            style={{
                                                backgroundColor: deepMode ? 'var(--accent)' : 'var(--border)',
                                                padding: '2px',
                                            }}
                                        >
                                            <span
                                                className="w-3 h-3 rounded-full bg-white shadow transition-transform"
                                                style={{ transform: deepMode ? 'translateX(12px)' : 'translateX(0)' }}
                                            />
                                        </span>
                                    </button>
                                </div>

                                {/* Divider */}
                                <div style={{ height: '1px', backgroundColor: 'var(--border-subtle)', margin: '4px 8px' }} />

                                {/* Profile section */}
                                <div className="px-2 pb-2">
                                    <p className="text-[0.6rem] font-semibold uppercase tracking-widest px-1 pt-1 pb-1" style={{ color: 'var(--text-faint)' }}>
                                        {t.researchProfile ?? 'Profile'}
                                    </p>
                                    {profiles.map(({ value, label, Icon }) => (
                                        <button
                                            key={value}
                                            type="button"
                                            onClick={() => { setResearchProfile(value); setPlusOpen(false) }}
                                            className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-xs font-semibold transition-all"
                                            style={researchProfile === value ? {
                                                backgroundColor: 'var(--accent-bg)',
                                                color: 'var(--accent)',
                                            } : {
                                                backgroundColor: 'transparent',
                                                color: 'var(--text-secondary)',
                                            }}
                                            onMouseEnter={(e) => { if (researchProfile !== value) e.currentTarget.style.backgroundColor = 'var(--surface-hover)' }}
                                            onMouseLeave={(e) => { if (researchProfile !== value) e.currentTarget.style.backgroundColor = 'transparent' }}
                                        >
                                            <Icon className="w-3.5 h-3.5 shrink-0" />
                                            {label}
                                            {researchProfile === value && (
                                                <svg className="w-3 h-3 ml-auto shrink-0" viewBox="0 0 12 12" fill="currentColor">
                                                    <path d="M10.28 2.28L4.5 8.06 1.72 5.28a1 1 0 0 0-1.44 1.44l3.5 3.5a1 1 0 0 0 1.44 0l6.5-6.5A1 1 0 0 0 10.28 2.28z" />
                                                </svg>
                                            )}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                </div>

                {/* Right: model info + settings */}
                <div className="flex items-center gap-2.5 shrink-0">
                    <span
                        className={`${isHero ? 'text-[0.7rem] max-w-[150px]' : 'text-[0.65rem] max-w-[120px]'} font-mono truncate`}
                        style={{ color: 'var(--text-faint)' }}
                    >
                        {provider === 'openai' ? `OpenAI · ${openaiModel}` : `Ollama · ${selectedModel}`}
                    </span>
                    <button
                        type="button"
                        onClick={onOpenSettings}
                        className="transition-colors shrink-0"
                        style={{ color: 'var(--text-faint)' }}
                        onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--text-secondary)' }}
                        onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-faint)' }}
                        aria-label={t.settings}
                    >
                        <Settings className={isHero ? 'w-4 h-4' : 'w-3.5 h-3.5'} />
                    </button>
                </div>
            </div>
        </form>
    )
}
