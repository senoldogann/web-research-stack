import { NextRequest, NextResponse } from 'next/server'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

interface OpenAIModel {
    id: string
    object: string
    created: number
    owned_by: string
}

/**
 * Filter to only chat-completion-capable models.
 * Excludes embeddings, TTS, Whisper, DALL-E, legacy completions, etc.
 */
function isChatModel(id: string): boolean {
    const lower = id.toLowerCase()
    const excluded = [
        'embedding', 'tts', 'whisper', 'dall-e', 'instruct',
        'search', 'similarity', 'edit', 'insert', 'babbage',
        'davinci', 'ada', 'curie', 'moderat', 'realtime',
        'transcri', 'audio', 'text-',
    ]
    if (excluded.some((e) => lower.includes(e))) return false
    return (
        lower.startsWith('gpt') ||
        lower.startsWith('o1') ||
        lower.startsWith('o3') ||
        lower.startsWith('o4') ||
        lower.startsWith('chatgpt')
    )
}

export async function POST(request: NextRequest) {
    try {
        const body = await request.json()
        const { apiKey } = body as { apiKey?: string }

        if (!apiKey || typeof apiKey !== 'string') {
            return NextResponse.json({ error: 'API key required' }, { status: 400 })
        }

        const res = await fetch('https://api.openai.com/v1/models', {
            headers: {
                Authorization: `Bearer ${apiKey}`,
                'Content-Type': 'application/json',
            },
            signal: AbortSignal.timeout(10000),
        })

        if (!res.ok) {
            const err = await res.json().catch(() => ({})) as { error?: { message?: string } }
            return NextResponse.json(
                { error: err?.error?.message || 'Invalid API key' },
                { status: res.status }
            )
        }

        const data = await res.json() as { data: OpenAIModel[] }
        const models = (data.data ?? [])
            .filter((m) => isChatModel(m.id))
            .sort((a, b) => b.created - a.created)
            .map((m) => m.id)

        return NextResponse.json({ models })
    } catch {
        return NextResponse.json({ error: 'Failed to fetch models' }, { status: 500 })
    }
}
