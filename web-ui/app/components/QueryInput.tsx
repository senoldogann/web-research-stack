'use client'

import { ArrowUpRight, Sparkles, Square, Settings, Plus } from 'lucide-react'
import { useLanguage } from '../contexts/LanguageProvider'

interface QueryInputProps {
    variant: 'hero' | 'chat'
    input: string
    setInput: (val: string) => void
    isLoading: boolean
    deepMode: boolean
    setDeepMode: (val: boolean) => void
    researchProfile: 'technical' | 'news' | 'academic'
    setResearchProfile: (val: 'technical' | 'news' | 'academic') => void
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

    const profiles: { value: 'technical' | 'news' | 'academic'; label: string; emoji: string }[] = [
        { value: 'technical', label: t.profileTechnical, emoji: '⚙️' },
        { value: 'news', label: t.profileNews, emoji: '📰' },
        { value: 'academic', label: t.profileAcademic, emoji: '🎓' },
    ]

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
                    // Use inline styles for theme-aware colors
                    // Tailwind placeholder-* classes use hardcoded colors, so we override via CSS
                    // The placeholder color is handled by globals.css textarea::placeholder rule
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

            {/* Bottom toolbar — + Deep Mode + Profiles INSIDE the input box */}
            <div className="flex items-center justify-between px-3 pb-2.5 gap-2">
                <div className="flex items-center gap-1.5 flex-wrap">

                    {/* + anchor */}
                    <span
                        className="flex items-center justify-center w-6 h-6 rounded-lg shrink-0"
                        style={{ backgroundColor: 'var(--surface-hover)', color: 'var(--text-muted)', border: '1px solid var(--border-subtle)' }}
                    >
                        <Plus className="w-3 h-3" />
                    </span>

                    {/* Deep Mode toggle */}
                    <button
                        type="button"
                        onClick={() => setDeepMode(!deepMode)}
                        className={`flex items-center gap-1 ${isHero ? 'px-2.5' : 'px-2'} py-1 rounded-lg ${
                            isHero ? 'text-xs' : 'text-[0.68rem]'
                        } font-semibold transition-all`}
                        style={deepMode ? {
                            backgroundColor: 'var(--accent-bg)', color: 'var(--accent)',
                            borderWidth: '1px', borderStyle: 'solid', borderColor: 'var(--accent-border)',
                        } : {
                            backgroundColor: 'var(--surface-hover)', color: 'var(--text-muted)',
                            borderWidth: '1px', borderStyle: 'solid', borderColor: 'var(--border-subtle)',
                        }}
                        title={t.deepMode}
                    >
                        <Sparkles className={`w-3 h-3 ${deepMode ? 'opacity-100' : 'opacity-50'}`} />
                        {t.deepMode}
                    </button>

                    <span style={{ color: 'var(--border-subtle)', fontSize: '0.5rem' }}>●</span>

                    {/* Profile pills */}
                    <div className="flex items-center rounded-lg overflow-hidden" style={{ border: '1px solid var(--border-subtle)' }}>
                        {profiles.map((p) => (
                            <button
                                key={p.value}
                                type="button"
                                onClick={() => setResearchProfile(p.value)}
                                className={`${isHero ? 'px-2.5 text-xs' : 'px-2 text-[0.65rem]'} py-1 font-semibold transition-all`}
                                style={researchProfile === p.value ? {
                                    backgroundColor: 'var(--accent-bg)', color: 'var(--accent)',
                                } : {
                                    backgroundColor: 'transparent', color: 'var(--text-ghost)',
                                }}
                                title={`${t.researchProfile}: ${p.label}`}
                            >
                                <span className="mr-0.5">{p.emoji}</span>{p.label}
                            </button>
                        ))}
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
