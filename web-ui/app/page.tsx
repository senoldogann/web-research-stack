'use client'

// --- Helpers ---
const safeHostname = (url: string) => {
    try {
        return new URL(url).hostname;
    } catch {
        return url;
    }
};

const isSafeUrl = (url: string): boolean => {
    try {
        const { protocol } = new URL(url);
        return protocol === 'https:' || protocol === 'http:';
    } catch {
        return false;
    }
};

import { useEffect, useRef, useState, useMemo, type ReactNode } from 'react'
import { AnimatePresence, motion } from "framer-motion"
import {
    ChevronDown,
    Globe,
    Search,
    CheckCircle,
    Copy,
    Check,
    ExternalLink,
    Rocket,
    FileText,
    GitBranch,
    Compass,
    BarChart3,
    Database,
    CloudDownload,
    FileCheck,
    Brain,
    Sparkles,
} from 'lucide-react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import SettingsModal from './components/SettingsModal'
import QueryInput from './components/QueryInput'
import remarkBreaks from 'remark-breaks'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { useLanguage } from './contexts/LanguageProvider'
import { useTheme } from './contexts/ThemeProvider'

interface CitedSource {
    url: string
    title: string
    source: string
}

interface DataTableRow {
    metric: string
    value: string
    source?: string
    date?: string
}

interface ResearchResult {
    query: string
    summary: string
    key_findings: string[]
    data_table?: DataTableRow[]
    detailed_analysis?: string
    recommendations?: string
    cited_sources?: CitedSource[]
    sources: {
        source: string
        url: string
        title: string
        content?: string
        relevance_score: number
        error?: string
    }[]
    sources_checked: number
    sources_succeeded: number
}

interface Message {
    id: string
    type: 'user' | 'assistant' | 'loading'
    content: string
    result?: ResearchResult
    statusLogs?: StatusLog[]
    timestamp: Date
}

interface SourceStatus {
    url: string
    title: string
    status: 'pending' | 'loading' | 'completed' | 'error'
}

type UnknownRecord = Record<string, unknown>
type MarkdownCodeProps = { className?: string; children?: ReactNode }
type MarkdownChildrenProps = { children?: ReactNode }
type MarkdownLinkProps = { children?: ReactNode; href?: string }
type OllamaTagsResponse = {
    models?: Array<{ name?: string }>
}

function isRecord(value: unknown): value is UnknownRecord {
    return typeof value === 'object' && value !== null
}

function getString(value: unknown, fallback = ''): string {
    return typeof value === 'string' ? value : fallback
}

function getNumber(value: unknown, fallback = 0): number {
    return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function getStringArray(value: unknown): string[] {
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function faviconUrl(url: string): string {
    try {
        return `https://s2.googleusercontent.com/s2/favicons?domain=${new URL(url).hostname}&sz=32`
    } catch {
        return 'https://s2.googleusercontent.com/s2/favicons?domain=example.com&sz=32'
    }
}

// ---------------------------------------------------------------------------
// Citation icon stack — replaces ugly [1][2][3] inline citation numbers
// ---------------------------------------------------------------------------

function CitationStack({ nums, sources }: { nums: number[]; sources: CitedSource[] }) {
    const validSources = [...new Set(nums)]
        .map(n => sources[n - 1])
        .filter((s): s is CitedSource => Boolean(s?.url))
    if (!validSources.length) return null
    return (
        <span
            className="inline-flex items-center align-middle mx-0.5"
            style={{ verticalAlign: 'middle', lineHeight: 1 }}
        >
            {validSources.map((src, i) => (
                <a
                    key={src.url + i}
                    href={isSafeUrl(src.url) ? src.url : '#'}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={src.title || src.url}
                    className="relative inline-flex items-center justify-center w-[18px] h-[18px] rounded-full overflow-hidden hover:scale-125 hover:z-10 transition-transform flex-shrink-0 text-[9px] font-bold"
                    style={{
                        marginLeft: i === 0 ? 0 : -5,
                        zIndex: validSources.length - i,
                        boxShadow: `0 0 0 1.5px var(--citation-ring)`,
                        backgroundColor: 'var(--citation-bg)',
                        color: 'var(--text-ghost)',
                    }}
                >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                        src={faviconUrl(src.url)}
                        alt=""
                        width={18}
                        height={18}
                        className="w-full h-full object-cover"
                        onError={(e) => { e.currentTarget.style.display = 'none' }}
                    />
                </a>
            ))}
        </span>
    )
}

/**
 * Strip backend warning annotations that would render as broken markdown links.
 * e.g. [1](⚠ Lower-authority source) → [1]
 * Also strips stray inline ⚠ parenthetical patterns anywhere in the text.
 */
function cleanMarkdown(text: string): string {
    return text.replace(/\]\(⚠[^)]*\)/g, ']')
}

function splitRecommendationBlocks(text: string): string[] {
    if (!text) return []
    return text
        .split(/\n\s*\n+/)
        .map((block) => block.trim())
        .filter(Boolean)
}

/** Recursively replace [N] citation markers in text-node children with CitationStack icons. */
function withCitations(children: ReactNode, sources: CitedSource[]): ReactNode {
    if (!sources.length) return children
    const processStr = (text: string): ReactNode => {
        const parts: ReactNode[] = []
        let lastIndex = 0
        const re = /(\[\d+\])+/g
        let m: RegExpExecArray | null
        while ((m = re.exec(text)) !== null) {
            if (m.index > lastIndex) parts.push(text.slice(lastIndex, m.index))
            const nums = [...m[0].matchAll(/\[(\d+)\]/g)].map(x => parseInt(x[1], 10))
            parts.push(<CitationStack key={`cit-${m.index}`} nums={nums} sources={sources} />)
            lastIndex = m.index + m[0].length
        }
        if (lastIndex < text.length) parts.push(text.slice(lastIndex))
        return parts.length === 1 && typeof parts[0] === 'string' ? parts[0] : parts
    }
    if (typeof children === 'string') return processStr(children)
    if (Array.isArray(children)) {
        return children.flatMap((child) => {
            if (typeof child === 'string') {
                const r = processStr(child)
                return Array.isArray(r) ? r : [r]
            }
            return [child]
        })
    }
    return children
}

function normalizeResearchResult(data: unknown): ResearchResult {
    const payload = isRecord(data) ? data : {}
    const metadata = isRecord(payload.metadata) ? payload.metadata : {}
    const rawSources = Array.isArray(payload.sources) ? payload.sources : []

    return {
        query: getString(payload.query),
        summary: getString(payload.summary) || getString(payload.answer),
        key_findings: getStringArray(payload.key_findings),
        data_table: Array.isArray(payload.data_table)
            ? (payload.data_table as unknown[])
                .filter(isRecord)
                .map(row => ({
                    metric: getString(row.metric),
                    value: getString(row.value),
                    source: getString(row.source) || undefined,
                    date: getString(row.date) || undefined,
                }))
                .filter(row => row.metric && row.value)
            : undefined,
        detailed_analysis: getString(payload.detailed_analysis),
        recommendations: getString(payload.recommendations),
        cited_sources: Array.isArray(payload.cited_sources)
            ? payload.cited_sources
                .filter(isRecord)
                .map(s => ({
                    url: getString(s.url),
                    title: getString(s.title),
                    source: getString(s.source),
                }))
                .filter(s => s.url)
            : undefined,
        sources: rawSources.map((source) => {
            const sourceRecord = isRecord(source) ? source : {}

            return {
                source: getString(sourceRecord.source, 'source'),
                url: getString(sourceRecord.url),
                title: getString(sourceRecord.title) || getString(sourceRecord.url) || 'Untitled source',
                content: typeof sourceRecord.content === 'string' ? sourceRecord.content : undefined,
                relevance_score: getNumber(sourceRecord.relevance_score),
                error: typeof sourceRecord.error === 'string' ? sourceRecord.error : undefined,
            }
        }),
        sources_checked: getNumber(payload.sources_checked, getNumber(metadata.sources_checked, rawSources.length)),
        sources_succeeded: getNumber(
            payload.sources_succeeded,
            getNumber(
                metadata.sources_succeeded,
                rawSources.filter((source) => !isRecord(source) || !source.error).length
            )
        ),
    }
}

interface StatusLog {
    id: string
    message: string
    type: 'info' | 'search' | 'success' | 'process'
    timestamp: Date
}

const CopyButton = ({ text }: { text: string }) => {
    const [copied, setCopied] = useState(false)
    const { t } = useLanguage()

    const handleCopy = async () => {
        try {
            await navigator.clipboard.writeText(text)
            setCopied(true)
            setTimeout(() => setCopied(false), 2000)
        } catch (err) {
            console.error('Failed to copy text: ', err)
        }
    }

    return (
        <button
            onClick={handleCopy}
            className="absolute top-3 right-3 p-2 rounded-lg transition-all z-10"
            style={{
                backgroundColor: 'var(--surface-overlay)',
                borderColor: 'var(--border)',
                color: 'var(--text-muted)',
            }}
            title={t.copySelection}
        >
            {copied ? (
                <Check className="w-4 h-4" style={{ color: 'var(--accent)' }} />
            ) : (
                <Copy className="w-4 h-4" />
            )}
        </button>
    )
}

const markdownComponents: Components = {
    code({ className, children }: MarkdownCodeProps) {
        const match = /language-(\w+)/.exec(className || '')
        const codeText = String(children).replace(/\n$/, '')
        const isInline = !match && !codeText.includes('\n') && !className?.includes('language-')

        if (isInline) {
            return (
                <code
                    className="px-1.5 py-0.5 rounded-md text-sm font-sans"
                    style={{
                        backgroundColor: 'var(--code-bg)',
                        color: 'var(--text-primary)',
                        border: '1px solid var(--border)',
                    }}
                >
                    {children}
                </code>
            )
        }

        return (
            <div className="group relative rounded-xl overflow-hidden shadow-sm my-6 text-sm" style={{ border: '1px solid var(--border)' }}>
                <div
                    className="flex items-center justify-between px-4 py-2"
                    style={{
                        backgroundColor: 'var(--code-block-header)',
                        borderBottom: '1px solid var(--border)',
                    }}
                >
                    <span className="text-xs font-mono uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                        {match ? match[1] : 'code'}
                    </span>
                </div>
                <CopyButton text={codeText} />
                <SyntaxHighlighter
                    style={vscDarkPlus}
                    language={match ? match[1] : 'text'}
                    PreTag="div"
                    customStyle={{ margin: 0, padding: '1.25rem', background: 'var(--code-block-bg)' }}
                >
                    {codeText}
                </SyntaxHighlighter>
            </div>
        )
    },
    br: () => <br className="my-1" />,
    h1: ({ children }: MarkdownChildrenProps) => <h1 className="text-2xl font-bold font-serif mt-8 mb-4 pb-2" style={{ color: 'var(--text-primary)', borderBottom: '1px solid var(--border)' }}>{children}</h1>,
    h2: ({ children }: MarkdownChildrenProps) => <h2 className="text-2xl font-bold font-serif mt-10 mb-5 pb-2" style={{ color: 'var(--text-primary)', borderBottom: '1px solid var(--border-subtle)' }}>{children}</h2>,
    h3: ({ children }: MarkdownChildrenProps) => <h3 className="text-xl font-bold font-serif mt-8 mb-4" style={{ color: 'var(--text-primary)' }}>{children}</h3>,
    h4: ({ children }: MarkdownChildrenProps) => <h4 className="text-lg font-bold font-serif mt-6 mb-3" style={{ color: 'var(--text-primary)' }}>{children}</h4>,
    h5: ({ children }: MarkdownChildrenProps) => <h5 className="text-base font-bold font-serif mt-5 mb-2" style={{ color: 'var(--text-primary)' }}>{children}</h5>,
    p: ({ children }: MarkdownChildrenProps) => <p className="font-serif leading-loose mb-6 text-[1.08rem]" style={{ color: 'var(--text-secondary)' }}>{children}</p>,
    ul: ({ children }: MarkdownChildrenProps) => <ul className="list-disc pl-6 space-y-3 mb-6 font-serif" style={{ color: 'var(--text-secondary)' }}>{children}</ul>,
    ol: ({ children }: MarkdownChildrenProps) => <ol className="list-decimal pl-6 space-y-3 mb-6 font-serif" style={{ color: 'var(--text-secondary)' }}>{children}</ol>,
    li: ({ children }: MarkdownChildrenProps) => <li className="pl-1.5 leading-loose text-[1.05rem]"><span className="font-medium" style={{ color: 'var(--text-primary)' }}>{children}</span></li>,
    a: ({ children, href }: MarkdownLinkProps) => <a href={href} target="_blank" rel="noopener noreferrer" className="underline underline-offset-4 transition-colors" style={{ color: 'var(--accent-muted)', textDecorationColor: 'var(--accent-border)' }}>{children}</a>,
    strong: ({ children }: MarkdownChildrenProps) => <strong className="font-bold" style={{ color: 'var(--text-primary)' }}>{children}</strong>,
    blockquote: ({ children }: MarkdownChildrenProps) => <blockquote className="italic pl-4 py-1 my-6 font-serif" style={{ borderLeft: '2px solid var(--accent)', color: 'var(--text-muted)' }}>{children}</blockquote>,
    table: ({ children }: MarkdownChildrenProps) => <div className="overflow-x-auto my-8" style={{ borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)' }}><table className="w-full text-left border-collapse text-[0.95rem]">{children}</table></div>,
    thead: ({ children }: MarkdownChildrenProps) => <thead style={{ backgroundColor: 'var(--bg-surface)', color: 'var(--text-primary)' }}>{children}</thead>,
    tbody: ({ children }: MarkdownChildrenProps) => <tbody>{children}</tbody>,
    tr: ({ children }: MarkdownChildrenProps) => <tr className="transition-colors" style={{ borderBottom: '1px solid var(--border)' }}>{children}</tr>,
    th: ({ children }: MarkdownChildrenProps) => <th className="px-6 py-4 font-semibold" style={{ borderBottom: '1px solid var(--border)' }}>{children}</th>,
    td: ({ children }: MarkdownChildrenProps) => <td className="px-6 py-4" style={{ color: 'var(--text-secondary)' }}>{children}</td>,
}

export default function Home() {
    const [messages, setMessages] = useState<Message[]>([])
    const [input, setInput] = useState('')
    const [isLoading, setIsLoading] = useState(false)
    const [statusLogs, setStatusLogs] = useState<StatusLog[]>([])
    const [models, setModels] = useState<string[]>([])
    const [deepMode, setDeepMode] = useState(false)
    const [researchProfile, setResearchProfile] = useState<'technical' | 'news' | 'academic' | 'auto'>('auto')
    const [abortController, setAbortController] = useState<AbortController | null>(null)
    const [activeSources, setActiveSources] = useState<SourceStatus[]>([])

    const { t } = useLanguage()
    const { resolvedTheme } = useTheme()

    // Translate backend status messages
    const translateStatusMessage = (message: string): string => {
        const lowerMsg = message.toLowerCase();
        
        if (lowerMsg.includes('preparing search')) {
            return t.researchStatusPreparingSearchQueries;
        }
        if (lowerMsg.includes('planning research')) {
            return t.researchStatusPlanningStrategy;
        }
        if (lowerMsg.includes('potential sources') && lowerMsg.includes('depth')) {
            const countMatch = message.match(/(\d+)/);
            const depthMatch = message.match(/depth:\s*(\w+)/i);
            return t.researchStatusFoundSources
                .replace('{count}', countMatch?.[1] || '0')
                .replace('{depth}', depthMatch?.[1] || 'standard');
        }
        if (lowerMsg.includes('gathering data')) {
            return t.researchStatusGatheringData;
        }
        if (lowerMsg.includes('analyzing') && lowerMsg.includes('synthes')) {
            return t.researchStatusAnalyzingFindings;
        }
        if (lowerMsg.includes('search query variants')) {
            const countMatch = message.match(/generated\s+(\d+)/i);
            return t.researchStatusGeneratingQueries.replace('{count}', countMatch?.[1] || '0');
        }
        
        return message; // Return original if no match
    };

    // Settings state — lazy-initialized from localStorage (SSR-safe)
    const [provider, setProvider] = useState<'ollama' | 'openai'>(() => {
        if (typeof window === 'undefined') return 'ollama'
        return (localStorage.getItem('provider') as 'ollama' | 'openai') || 'ollama'
    })
    const [openaiApiKey, setOpenaiApiKey] = useState(() => {
        if (typeof window === 'undefined') return ''
        return localStorage.getItem('openaiApiKey') || ''
    })
    const [openaiModel, setOpenaiModel] = useState(() => {
        if (typeof window === 'undefined') return 'gpt-4o'
        return localStorage.getItem('openaiModel') || 'gpt-4o'
    })
    const [selectedModel, setSelectedModel] = useState(() => {
        if (typeof window === 'undefined') return 'gpt-oss:120b-cloud'
        return localStorage.getItem('ollamaModel') || 'gpt-oss:120b-cloud'
    })
    const [ollamaBaseUrl, setOllamaBaseUrl] = useState(() => {
        if (typeof window === 'undefined') return ''
        return localStorage.getItem('ollamaBaseUrl') || ''
    })
    const [ollamaApiKey, setOllamaApiKey] = useState(() => {
        if (typeof window === 'undefined') return ''
        return localStorage.getItem('ollamaApiKey') || ''
    })

    const [showSettings, setShowSettings] = useState(false)
    const messagesEndRef = useRef<HTMLDivElement>(null)
    const hasMessages = messages.length > 0

    // Persist settings to localStorage whenever they change
    useEffect(() => { localStorage.setItem('provider', provider) }, [provider])
    useEffect(() => { localStorage.setItem('openaiApiKey', openaiApiKey) }, [openaiApiKey])
    useEffect(() => { localStorage.setItem('openaiModel', openaiModel) }, [openaiModel])
    useEffect(() => { localStorage.setItem('ollamaModel', selectedModel) }, [selectedModel])
    useEffect(() => { localStorage.setItem('ollamaBaseUrl', ollamaBaseUrl) }, [ollamaBaseUrl])
    useEffect(() => { localStorage.setItem('ollamaApiKey', ollamaApiKey) }, [ollamaApiKey])

    useEffect(() => {
        async function fetchModels() {
            try {
                const res = await fetch('/api/ollama/models', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        ollamaUrl: ollamaBaseUrl || undefined,
                        ollamaApiKey: ollamaApiKey || undefined,
                    }),
                })
                if (!res.ok) return
                const data: OllamaTagsResponse = await res.json()
                if (Array.isArray(data.models)) {
                    setModels(
                        data.models
                            .map((model) => model.name)
                            .filter((name): name is string => typeof name === 'string')
                    )
                }
            } catch (error) {
                console.error('Failed to fetch Ollama models:', error)
            }
        }
        fetchModels()
    }, [ollamaBaseUrl, ollamaApiKey])

    const scrollToBottom = () => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }

    useEffect(() => {
        scrollToBottom()
    }, [messages])

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault()
        if (!input.trim() || isLoading) return

        const userMessage: Message = {
            id: crypto.randomUUID(),
            type: 'user',
            content: input.trim(),
            timestamp: new Date(),
        }

        const loadingMessage: Message = {
            id: crypto.randomUUID(),
            type: 'loading',
            content: '',
            timestamp: new Date(),
        }

        setMessages((prev) => [...prev, userMessage, loadingMessage])
        setInput('')
        setIsLoading(true)
        const initialStatusLogs: StatusLog[] = [{
            id: crypto.randomUUID(),
            message: t.initiatingSearch,
            type: 'info',
            timestamp: new Date()
        }];
        setStatusLogs(initialStatusLogs)
        let currentStatusLogs = [...initialStatusLogs];
        setActiveSources([])

        const controller = new AbortController()
        setAbortController(controller)

        setTimeout(scrollToBottom, 50)

        try {
            const response = await fetch('/api/research/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                signal: controller.signal,
                body: JSON.stringify({
                    query: userMessage.content,
                    deepMode: deepMode,
                    research_profile: researchProfile,
                    model: provider === 'openai' ? openaiModel : selectedModel,
                    provider,
                    openaiApiKey: provider === 'openai' ? openaiApiKey : undefined,
                    ollamaApiKey: provider === 'ollama' && ollamaApiKey ? ollamaApiKey : undefined,
                    ollamaBaseUrl: provider === 'ollama' && ollamaBaseUrl ? ollamaBaseUrl : undefined,
                }),
            })

            if (!response.ok) {
                let errorMessage = `Research failed: ${response.status}`;
                try {
                    const errorText = await response.text();
                    const errorData = JSON.parse(errorText);
                    errorMessage = errorData.error || errorMessage;
                } catch (e) {
                    console.error("Failed to parse error response", e);
                }
                throw new Error(errorMessage);
            }

            if (!response.body) throw new Error('No response body')

            const reader = response.body.getReader()
            const decoder = new TextDecoder()
            let doneReading = false
            let buffer = ""

            while (!doneReading) {
                const { value, done } = await reader.read()
                doneReading = done
                if (value) {
                    const chunk = decoder.decode(value, { stream: true })
                    buffer += chunk
                    const events = buffer.split('\n\n')

                    buffer = events.pop() || ''

                    for (const event of events) {
                        const lines = event.split('\n')
                        for (const line of lines) {
                            if (line.startsWith('data: ')) {
                                const dataStr = line.substring(6).trim()
                                if (!dataStr || dataStr === '[DONE]') continue

                                try {
                                    const parsed = JSON.parse(dataStr)

                                    if (parsed.type === 'status') {
                                        if (currentStatusLogs.length > 0 && currentStatusLogs[currentStatusLogs.length - 1].message === parsed.message) continue;

                                        let type: StatusLog['type'] = 'info';
                                        if (parsed.message.toLowerCase().includes('search')) type = 'search';
                                        if (parsed.message.toLowerCase().includes('completed') || parsed.message.toLowerCase().includes('finished')) type = 'success';
                                        if (parsed.message.toLowerCase().includes('analysing') || parsed.message.toLowerCase().includes('selecting')) type = 'process';

                                        currentStatusLogs = [...currentStatusLogs, {
                                            id: crypto.randomUUID(),
                                            message: translateStatusMessage(parsed.message),
                                            type,
                                            timestamp: new Date()
                                        }];
                                        setStatusLogs(currentStatusLogs);
                                        scrollToBottom()
                                    } else if (parsed.type === 'source_start') {
                                        const source: SourceStatus = {
                                            url: parsed.url,
                                            title: parsed.title || safeHostname(parsed.url),
                                            status: 'loading'
                                        }
                                        setActiveSources(prev => [...prev, source])
                                        scrollToBottom()
                                    } else if (parsed.type === 'source_complete') {
                                        setActiveSources(prev => prev.map(s =>
                                            s.url === parsed.url
                                                ? { ...s, status: parsed.success ? 'completed' : 'error', title: parsed.title || safeHostname(parsed.url) }
                                                : s
                                        ))
                                    } else if (parsed.type === 'result') {
                                        const result: ResearchResult = normalizeResearchResult(parsed.data)
                                        setMessages((prev) =>
                                            prev.map((msg) =>
                                                msg.id === loadingMessage.id
                                                    ? {
                                                        ...msg,
                                                        type: 'assistant',
                                                        content: result.summary,
                                                        result,
                                                        statusLogs: currentStatusLogs,
                                                    }
                                                    : msg
                                            )
                                        )
                                    } else if (parsed.type === 'error') {
                                        setMessages(prev => prev.map(msg =>
                                            msg.id === loadingMessage.id
                                                ? {
                                                    ...msg,
                                                    type: 'assistant',
                                                    content: parsed.message || 'Research failed. Please check your settings and try again.',
                                                    statusLogs: currentStatusLogs,
                                                }
                                                : msg
                                        ))
                                    }
                                } catch (e) {
                                    if (process.env.NODE_ENV === 'development') {
                                        console.warn("Malformed SSE event dropped:", dataStr, e)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        } catch (error: unknown) {
            if (error instanceof Error && error.name === 'AbortError') {
                console.log(t.searchAborted)
            } else {
                console.error('Search error:', error)
                setMessages(prev => [...prev, {
                    id: crypto.randomUUID(),
                    type: 'assistant',
                    content: t.researchError.replace('{error}', error instanceof Error ? error.message : String(error)),
                    timestamp: new Date()
                }])
            }
        } finally {
            setIsLoading(false)
            setAbortController(null)
            setMessages(prev => prev.map(msg =>
                msg.id === loadingMessage.id && msg.type === 'loading'
                    ? {
                        ...msg,
                        type: 'assistant',
                        content: t.connectionInterrupted,
                    }
                    : msg
            ))
            setTimeout(scrollToBottom, 50)
        }
    }

    const handleStop = () => {
        if (abortController) {
            abortController.abort()
        }
    }

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            handleSubmit(e)
        }
    }

    // Dynamic syntax highlighter style based on theme
    const codeStyle = resolvedTheme === 'light' ? undefined : vscDarkPlus

    // Memoize themed markdown components for light mode code blocks
    const themedMarkdownComponents = useMemo((): Components => {
        if (resolvedTheme === 'dark') return markdownComponents
        return {
            ...markdownComponents,
            code({ className, children }: MarkdownCodeProps) {
                const match = /language-(\w+)/.exec(className || '')
                const codeText = String(children).replace(/\n$/, '')
                const isInline = !match && !codeText.includes('\n') && !className?.includes('language-')

                if (isInline) {
                    return (
                        <code
                            className="px-1.5 py-0.5 rounded-md text-sm font-sans"
                            style={{
                                backgroundColor: 'var(--code-bg)',
                                color: 'var(--text-primary)',
                                border: '1px solid var(--border)',
                            }}
                        >
                            {children}
                        </code>
                    )
                }

                return (
                    <div className="group relative rounded-xl overflow-hidden shadow-sm my-6 text-sm" style={{ border: '1px solid var(--border)' }}>
                        <div
                            className="flex items-center justify-between px-4 py-2"
                            style={{
                                backgroundColor: 'var(--code-block-header)',
                                borderBottom: '1px solid var(--border)',
                            }}
                        >
                            <span className="text-xs font-mono uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                                {match ? match[1] : 'code'}
                            </span>
                        </div>
                        <CopyButton text={codeText} />
                        <SyntaxHighlighter
                            style={codeStyle}
                            language={match ? match[1] : 'text'}
                            PreTag="div"
                            customStyle={{ margin: 0, padding: '1.25rem', background: 'var(--code-block-bg)' }}
                        >
                            {codeText}
                        </SyntaxHighlighter>
                    </div>
                )
            },
        }
    }, [resolvedTheme, codeStyle])

    return (
        <main className="min-h-screen flex flex-col relative font-sans" style={{ backgroundColor: 'var(--bg-primary)', color: 'var(--text-primary)' }}>
            <div className="flex-1 w-full max-w-3xl mx-auto px-4 sm:px-6 flex flex-col">
                {/* Hero Section */}
                <AnimatePresence>
                    {!hasMessages && (
                        <motion.section
                            initial={{ opacity: 1, scale: 1 }}
                            exit={{ opacity: 0, scale: 0.95, filter: 'blur(10px)' }}
                            transition={{ duration: 0.4 }}
                            className="flex-1 flex flex-col items-center justify-center min-h-[70vh] pb-[10vh]"
                        >
                            <motion.h1
                                initial={{ opacity: 0, y: 10 }}
                                animate={{ opacity: 1, y: 0 }}
                                className="text-[2.5rem] font-serif mb-8 flex items-center gap-3"
                                style={{ color: 'var(--text-primary)' }}
                            >
                                <Search className="w-5 h-5 flex-shrink-0 mt-0.5" style={{ color: 'var(--accent-muted)', opacity: 0.6 }} />
                                {t.heroTitle}
                            </motion.h1>

                            <div className="w-full relative shadow-2xl">
                                <QueryInput
                                    variant="hero"
                                    input={input}
                                    setInput={setInput}
                                    isLoading={isLoading}
                                    deepMode={deepMode}
                                    setDeepMode={setDeepMode}
                                    researchProfile={researchProfile}
                                    setResearchProfile={setResearchProfile}
                                    provider={provider}
                                    openaiModel={openaiModel}
                                    selectedModel={selectedModel}
                                    onSubmit={handleSubmit}
                                    onStop={handleStop}
                                    onKeyDown={handleKeyDown}
                                    onOpenSettings={() => setShowSettings(true)}
                                />
                            </div>
                        </motion.section>
                    )}
                </AnimatePresence>

                {/* Chat Area */}
                <section className={`flex-1 pb-40 ${hasMessages ? 'pt-8' : 'hidden'}`}>
                    <div className="space-y-10 mb-8">
                        <AnimatePresence>
                            {messages.map((message) => (
                                <motion.div
                                    key={message.id}
                                    initial={{ opacity: 0, y: 15 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    exit={{ opacity: 0 }}
                                    transition={{ duration: 0.3 }}
                                    className={`flex w-full ${message.type === 'user' ? 'justify-end' : 'justify-start'}`}
                                >
                                    {message.type === 'loading' ? (
                                        <LoadingState
                                            sources={activeSources}
                                            statusLogs={statusLogs}
                                            isCompleted={false}
                                        />
                                    ) : message.type === 'user' ? (
                                        <div
                                            className="max-w-[85%] rounded-3xl rounded-tr-lg px-5 py-4 shadow-lg"
                                            style={{
                                                backgroundColor: 'var(--bg-primary)',
                                                border: '1px solid var(--border-muted)',
                                                color: 'var(--text-primary)',
                                            }}
                                        >
                                            <p className="text-[1rem] leading-relaxed font-sans">{message.content}</p>
                                        </div>
                                    ) : (
                                        <div className="w-full flex gap-4 max-w-[95%]">
                                            <div className="flex-1 min-w-0">
                                                {message.result && (
                                                    <LoadingState
                                                        sources={message.result.sources.map(s => ({
                                                            url: s.url,
                                                            title: s.title,
                                                            status: s.error ? 'error' as const : 'completed' as const
                                                        }))}
                                                        statusLogs={message.statusLogs || []}
                                                        isCompleted={true}
                                                    />
                                                )}
                                                {message.result ? (
                                                    <ResearchResultCard result={message.result} markdownComponents={themedMarkdownComponents} />
                                                ) : (
                                                    <div className="prose max-w-none font-serif prose-p:text-[1.05rem]" style={{ color: 'var(--text-secondary)' }}>
                                                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={themedMarkdownComponents}>
                                                            {message.content.replace(/ \|\| /g, '\n|')}
                                                        </ReactMarkdown>
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    )}
                                </motion.div>
                            ))}
                        </AnimatePresence>
                        <div ref={messagesEndRef} className="h-6" />
                    </div>
                </section>
            </div>

            {/* Fixed Input Block - Visible when chatting */}
            <AnimatePresence>
                {
                    hasMessages && (
                        <motion.div
                            initial={{ y: 20, opacity: 0 }}
                            animate={{ y: 0, opacity: 1 }}
                            className="fixed bottom-0 left-0 right-0 z-50 pointer-events-none pb-6 pt-12"
                        >
                            {/* Gradient Mask */}
                            <div
                                className="absolute bottom-0 left-0 right-0 h-full pointer-events-none"
                                style={{
                                    background: `linear-gradient(to top, var(--bg-primary) 0%, var(--bg-primary) 50%, transparent 100%)`,
                                }}
                            />

                            <div className="relative max-w-3xl mx-auto px-4 sm:px-6 pointer-events-auto">
                                <QueryInput
                                    variant="chat"
                                    input={input}
                                    setInput={setInput}
                                    isLoading={isLoading}
                                    deepMode={deepMode}
                                    setDeepMode={setDeepMode}
                                    researchProfile={researchProfile}
                                    setResearchProfile={setResearchProfile}
                                    provider={provider}
                                    openaiModel={openaiModel}
                                    selectedModel={selectedModel}
                                    onSubmit={handleSubmit}
                                    onStop={handleStop}
                                    onKeyDown={handleKeyDown}
                                    onOpenSettings={() => setShowSettings(true)}
                                />
                                <p className="text-center text-[0.7rem] font-medium mt-3" style={{ color: 'var(--text-ghost)' }}>
                                    {t.aiDisclaimer}
                                </p>
                            </div>
                        </motion.div>
                    )
                }
            </AnimatePresence>

            {/* Settings Modal */}
            <SettingsModal
                open={showSettings}
                onClose={() => setShowSettings(false)}
                provider={provider}
                setProvider={setProvider}
                selectedModel={selectedModel}
                setSelectedModel={setSelectedModel}
                openaiModel={openaiModel}
                setOpenaiModel={setOpenaiModel}
                openaiApiKey={openaiApiKey}
                setOpenaiApiKey={setOpenaiApiKey}
                ollamaModels={models}
                ollamaBaseUrl={ollamaBaseUrl}
                setOllamaBaseUrl={setOllamaBaseUrl}
                ollamaApiKey={ollamaApiKey}
                setOllamaApiKey={setOllamaApiKey}
            />
        </main>
    )
}

function LoadingState({ sources, statusLogs, isCompleted }: { sources: SourceStatus[]; statusLogs: StatusLog[]; isCompleted?: boolean }) {
    const [isTraceExpanded, setIsTraceExpanded] = useState(true);
    const [isSourcesExpanded, setIsSourcesExpanded] = useState(true);
    const hasSources = sources.length > 0;
    const { t } = useLanguage()

    const getIcon = (type: StatusLog['type'], message: string, isLast: boolean, isProcessing: boolean) => {
        const color = isProcessing ? 'var(--text-secondary)' : 'var(--text-ghost)';
        const lowerMessage = message.toLowerCase();
        
        // Araştırma başlatma - Rocket/Sparkles
        if (lowerMessage.includes('başlatılıyor') || lowerMessage.includes('starting')) {
            return <Rocket className="w-4 h-4" style={{ color }} />;
        }
        
        // Sorgu hazırlama - FileText
        if (lowerMessage.includes('sorguları hazırlanıyor') || lowerMessage.includes('preparing search')) {
            return <FileText className="w-4 h-4" style={{ color }} />;
        }
        
        // Varyant oluşturma - GitBranch
        if (lowerMessage.includes('varyantı') || lowerMessage.includes('variants') || lowerMessage.includes('query variant')) {
            return <GitBranch className="w-4 h-4" style={{ color }} />;
        }
        
        // Strateji planlama - Compass
        if (lowerMessage.includes('stratejisi') || lowerMessage.includes('strategy') || lowerMessage.includes('planlanıyor')) {
            return <Compass className="w-4 h-4" style={{ color }} />;
        }
        
        // Sıralama ve seçme - BarChart3/Target
        if (lowerMessage.includes('sıralanıp') || lowerMessage.includes('ranking') || lowerMessage.includes('selecting')) {
            return <BarChart3 className="w-4 h-4" style={{ color }} />;
        }
        
        // Kaynak bulma - Database/Layers
        if (lowerMessage.includes('kaynak bulundu') || lowerMessage.includes('sources found') || lowerMessage.includes('potential sources')) {
            return <Database className="w-4 h-4" style={{ color }} />;
        }
        
        // Veri toplama - CloudDownload
        if (lowerMessage.includes('veri toplanıyor') || lowerMessage.includes('gathering data') || lowerMessage.includes('toplanıyor')) {
            return <CloudDownload className="w-4 h-4" style={{ color }} />;
        }
        
        // Karakter toplandı - FileCheck
        if (lowerMessage.includes('karakter') || lowerMessage.includes('characters')) {
            return <FileCheck className="w-4 h-4" style={{ color }} />;
        }
        
        // Analiz - Brain/Scan
        if (lowerMessage.includes('analiz') || lowerMessage.includes('analyzing') || lowerMessage.includes('sentez')) {
            return <Brain className="w-4 h-4" style={{ color }} />;
        }
        
        // Arama tipi için Globe
        if (type === 'search') {
            return <Globe className="w-4 h-4" style={{ color }} />;
        }
        
        // Başarı tipi için CheckCircle
        if (type === 'success') {
            return <CheckCircle className="w-4 h-4" style={{ color }} />;
        }
        
        // Varsayılan - Sparkles (sihirli efekt)
        return <Sparkles className="w-4 h-4" style={{ color }} />;
    };

    return (
        <div className={`font-sans w-full ${isCompleted ? 'mb-4 pb-4' : ''}`} style={isCompleted ? { borderBottom: '1px solid var(--border-subtle)' } : undefined}>
            {/* Header Toggle */}
            <button
                onClick={() => setIsTraceExpanded(!isTraceExpanded)}
                className="flex items-center gap-2 mb-4 px-2 py-1 rounded-lg transition-colors group"
                style={{ backgroundColor: 'transparent' }}
                onMouseEnter={e => (e.currentTarget.style.backgroundColor = 'var(--surface-hover)')}
                onMouseLeave={e => (e.currentTarget.style.backgroundColor = 'transparent')}
            >
                <span className={`text-[0.7rem] font-bold uppercase tracking-widest transition-colors ${!isCompleted ? 'animate-shimmer-text' : ''}`} style={isCompleted ? { color: 'var(--text-faint)' } : { color: 'var(--accent-muted)' }}>
                    {isCompleted ? t.researchCompleted : t.researching}
                </span>
                <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-300 ${isTraceExpanded ? 'rotate-180' : ''}`} style={{ color: 'var(--text-faint)' }} />
            </button>

            <AnimatePresence>
                {isTraceExpanded && (
                    <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden"
                    >
                        {/* Thinking History Trace */}
                        <div className="flex flex-col gap-0 ml-1">
                            {statusLogs.map((log, index) => {
                                const isLast = index === statusLogs.length - 1;
                                const isProcessing = isLast && !isCompleted;

                                return (
                                    <div key={log.id} className="flex flex-col">
                                        <div className="flex items-start gap-4 group">
                                            <div className="flex flex-col items-center">
                                                <div
                                                    className="mt-1.5 rounded-full p-0.5 transition-colors"
                                                    style={isProcessing ? { backgroundColor: 'var(--accent-bg)', boxShadow: `0 0 0 1px var(--accent-border)` } : undefined}
                                                >
                                                    {getIcon(log.type, log.message, isLast, isProcessing)}
                                                </div>
                                                {!isLast && (
                                                    <div className="w-[1px] h-full min-h-[1.2rem]" style={{ backgroundColor: 'var(--border-faint)' }} />
                                                )}
                                            </div>
                                            <div className="mt-1 flex flex-col pb-4">
                                                <span
                                                    className={`text-[0.85rem] transition-colors ${isProcessing ? 'font-medium animate-shimmer-text' : ''}`}
                                                    style={!isProcessing ? { color: 'var(--text-ghost)' } : { color: 'var(--text-primary)' }}
                                                >
                                                    {log.message}
                                                </span>
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>

                        {/* Source Container */}
                        {hasSources && (
                            <div className="ml-9 mt-0 mb-6 max-w-2xl">
                                <div className="overflow-hidden rounded-xl shadow-sm" style={{ border: '1px solid var(--border-subtle)', backgroundColor: 'var(--bg-secondary)' }}>
                                    <button
                                        className="w-full flex items-center justify-between px-4 py-2.5 transition-colors"
                                        style={{ borderBottom: '1px solid var(--border-faint)' }}
                                        onClick={() => setIsSourcesExpanded(!isSourcesExpanded)}
                                        onMouseEnter={e => (e.currentTarget.style.backgroundColor = 'var(--surface-hover)')}
                                        onMouseLeave={e => (e.currentTarget.style.backgroundColor = 'transparent')}
                                    >
                                        <div className="flex items-center gap-2">
                                            <Globe className="w-3.5 h-3.5" style={{ color: 'var(--accent-muted)', opacity: 0.6 }} />
                                            <span className="text-[0.8rem] font-medium" style={{ color: 'var(--text-muted)' }}>
                                                {t.sourcesFound.replace('{count}', String(sources.length))}
                                            </span>
                                        </div>
                                        <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-300 ${isSourcesExpanded ? 'rotate-180' : ''}`} style={{ color: 'var(--text-faint)' }} />
                                    </button>

                                    <AnimatePresence>
                                        {isSourcesExpanded && (
                                            <motion.div
                                                initial={{ height: 0, opacity: 0 }}
                                                animate={{ height: 'auto', opacity: 1 }}
                                                exit={{ height: 0, opacity: 0 }}
                                                className="overflow-hidden"
                                            >
                                                <div className="p-3 grid grid-cols-1 gap-1.5 max-h-60 overflow-y-auto custom-scrollbar" style={{ backgroundColor: 'var(--bg-secondary)' }}>
                                                    {sources.map((source, sIdx) => (
                                                        <div
                                                            key={sIdx}
                                                            className="flex items-center justify-between p-2 rounded-lg group transition-all"
                                                            style={{ border: '1px solid transparent' }}
                                                            onMouseEnter={e => {
                                                                e.currentTarget.style.backgroundColor = 'var(--surface-hover)';
                                                                e.currentTarget.style.borderColor = 'var(--border-faint)';
                                                            }}
                                                            onMouseLeave={e => {
                                                                e.currentTarget.style.backgroundColor = 'transparent';
                                                                e.currentTarget.style.borderColor = 'transparent';
                                                            }}
                                                        >
                                                            <div className="flex items-center gap-3 overflow-hidden flex-1">
                                                                {/* eslint-disable-next-line @next/next/no-img-element */}
                                                                <img src={faviconUrl(source.url)} className="w-4 h-4 rounded-sm opacity-60 flex-shrink-0" alt="" />
                                                                <div className="relative overflow-hidden flex-1 min-w-0">
                                                                    <span className="text-[0.8rem] truncate block transition-colors" style={{ color: 'var(--text-muted)' }}>
                                                                        {source.title}
                                                                    </span>
                                                                    {!isCompleted && <div className="source-shine-overlay" />}
                                                                </div>
                                                            </div>
                                                            <div className="flex items-center gap-3 ml-4 flex-shrink-0">
                                                                <span className="text-[0.7rem] font-mono hidden sm:inline" style={{ color: 'var(--text-placeholder)' }}>
                                                                    {safeHostname(source.url).replace('www.', '')}
                                                                </span>
                                                                <a href={isSafeUrl(source.url) ? source.url : '#'} target="_blank" rel="noopener noreferrer" className="opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded">
                                                                    <ExternalLink className="w-3.5 h-3.5" style={{ color: 'var(--text-faint)' }} />
                                                                </a>
                                                            </div>
                                                        </div>
                                                    ))}
                                                </div>
                                            </motion.div>
                                        )}
                                    </AnimatePresence>
                                </div>
                            </div>
                        )}
                    </motion.div>
                )}
            </AnimatePresence>
        </div>
    );
}

function ResearchResultCard({ result, markdownComponents: mdComponents }: { result: ResearchResult; markdownComponents: Components }) {
    const { t } = useLanguage()

    const citedComponents = useMemo((): Components => {
        const cited = result.cited_sources ?? []
        return {
            ...mdComponents,
            p: ({ children }: MarkdownChildrenProps) => (
                <p className="font-serif leading-relaxed mb-5 text-[1.05rem]" style={{ color: 'var(--text-secondary)' }}>
                    {withCitations(children, cited)}
                </p>
            ),
            li: ({ children }: MarkdownChildrenProps) => (
                <li className="pl-1 leading-relaxed">
                    <span className="font-medium" style={{ color: 'var(--text-primary)' }}>{withCitations(children, cited)}</span>
                </li>
            ),
        }
    }, [result.cited_sources, mdComponents])

    return (
        <div className="w-full font-serif space-y-8 animate-in fade-in slide-in-from-bottom-2 duration-500">
            {/* Header intro */}
            <p className="text-sm font-sans flex items-center gap-1.5 ml-1" style={{ color: 'var(--text-muted)' }}>
                {t.synthesizedIntro} <span style={{ opacity: 0.5 }}>›</span>
            </p>

            {/* Executive summary */}
            <div className="prose max-w-none prose-p:text-[1.08rem]" style={{ color: 'var(--text-secondary)' }}>
                <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={citedComponents}>
                    {cleanMarkdown(result.summary)}
                </ReactMarkdown>
            </div>

            {/* Key Findings */}
            {result.key_findings && result.key_findings.length > 0 && (
                <div className="pt-6" style={{ borderTop: '1px solid var(--border-subtle)' }}>
                    <p className="text-[0.7rem] font-sans font-semibold tracking-widest uppercase mb-4" style={{ color: 'var(--accent-muted)' }}>
                        {t.keyFindings}
                    </p>
                    <ul className="space-y-3 list-none pl-0">
                        {result.key_findings.map((finding, i) => {
                            const cited = result.cited_sources ?? []
                            return (
                                <li
                                    key={i}
                                    className="rounded-lg px-4 py-3"
                                    style={{
                                        border: '1px solid var(--border-subtle)',
                                        background: 'var(--bg-surface)',
                                    }}
                                >
                                    <div className="flex items-start gap-3 text-[1rem] leading-relaxed font-serif" style={{ color: 'var(--text-secondary)' }}>
                                        <span
                                            className="inline-flex h-6 min-w-6 items-center justify-center rounded-full px-2 text-[0.72rem] font-semibold"
                                            style={{
                                                color: 'var(--text-primary)',
                                                background: 'var(--badge-bg)',
                                                border: '1px solid var(--border)',
                                            }}
                                        >
                                            {i + 1}
                                        </span>
                                        <span>{withCitations(cleanMarkdown(finding), cited)}</span>
                                    </div>
                                </li>
                            )
                        })}
                    </ul>
                </div>
            )}

            {/* Detailed analysis */}
            {result.detailed_analysis && (
                <div className="pt-6 prose max-w-none prose-p:text-[1.08rem]" style={{ borderTop: '1px solid var(--border-subtle)', color: 'var(--text-secondary)' }}>
                    <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={citedComponents}>
                        {cleanMarkdown(result.detailed_analysis)}
                    </ReactMarkdown>
                </div>
            )}

            {/* Recommendations */}
            {result.recommendations && (
                <div className="pt-6" style={{ borderTop: '1px solid var(--border-subtle)' }}>
                    <p className="text-[0.7rem] font-sans font-semibold tracking-widest uppercase mb-4" style={{ color: 'var(--accent-muted)' }}>
                        {t.recommendations}
                    </p>
                    <div className="space-y-3">
                        {splitRecommendationBlocks(result.recommendations).map((recommendation, index) => (
                            <div
                                key={index}
                                className="rounded-lg px-4 py-3"
                                style={{
                                    border: '1px solid var(--border-subtle)',
                                    background: 'var(--bg-surface)',
                                }}
                            >
                                <div className="flex items-start gap-3">
                                    <span
                                        className="inline-flex h-6 min-w-6 items-center justify-center rounded-full px-2 text-[0.72rem] font-semibold"
                                        style={{
                                            color: 'var(--text-primary)',
                                            background: 'var(--badge-bg)',
                                            border: '1px solid var(--border)',
                                        }}
                                    >
                                        {index + 1}
                                    </span>
                                    <div className="prose max-w-none prose-p:text-[1rem]" style={{ color: 'var(--text-secondary)' }}>
                                        <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={citedComponents}>
                                            {cleanMarkdown(recommendation)}
                                        </ReactMarkdown>
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Data table (benchmark / structured results) */}
            {result.data_table && result.data_table.length > 0 && (
                <div className="pt-6" style={{ borderTop: '1px solid var(--border-subtle)' }}>
                    <p className="text-[0.7rem] font-sans font-semibold tracking-widest uppercase mb-4" style={{ color: 'var(--accent-muted)' }}>
                        Veriler
                    </p>
                    <div className="overflow-x-auto rounded-lg" style={{ border: '1px solid var(--border)' }}>
                        <table className="w-full text-left border-collapse">
                            <thead>
                                <tr style={{ backgroundColor: 'var(--bg-surface)', borderBottom: '2px solid var(--border)' }}>
                                    <th className="px-5 py-3 text-[0.78rem] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-primary)' }}>Metrik</th>
                                    <th className="px-5 py-3 text-[0.78rem] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-primary)' }}>Sonuç</th>
                                    {result.data_table.some(r => r.source) && <th className="px-5 py-3 text-[0.78rem] font-semibold uppercase tracking-wider hidden sm:table-cell" style={{ color: 'var(--text-primary)' }}>Kaynak</th>}
                                    {result.data_table.some(r => r.date) && <th className="px-5 py-3 text-[0.78rem] font-semibold uppercase tracking-wider hidden sm:table-cell" style={{ color: 'var(--text-primary)' }}>Tarih</th>}
                                </tr>
                            </thead>
                            <tbody>
                                {result.data_table.map((row, i) => (
                                    <tr key={i} style={{ borderBottom: i < result.data_table!.length - 1 ? '1px solid var(--border-subtle)' : undefined }}>
                                        <td className="px-5 py-3 text-[0.85rem] font-medium" style={{ color: 'var(--text-primary)' }}>{row.metric}</td>
                                        <td className="px-5 py-3 text-[0.85rem] font-sans" style={{ color: 'var(--text-secondary)' }}>{row.value}</td>
                                        {result.data_table!.some(r => r.source) && <td className="px-5 py-3 text-[0.85rem] hidden sm:table-cell" style={{ color: 'var(--text-muted)' }}>{row.source}</td>}
                                        {result.data_table!.some(r => r.date) && <td className="px-5 py-3 text-[0.85rem] hidden sm:table-cell" style={{ color: 'var(--text-muted)' }}>{row.date}</td>}
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}
        </div>
    )
}
