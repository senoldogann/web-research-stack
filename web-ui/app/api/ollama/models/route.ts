import { NextResponse } from 'next/server'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

/**
 * Server-side proxy for Ollama /api/tags
 * Avoids CSP violations from browser fetching localhost:11434 directly.
 */
export async function GET() {
    const ollamaUrl = process.env.OLLAMA_URL || process.env.NEXT_PUBLIC_OLLAMA_URL || 'http://localhost:11434'
    try {
        const res = await fetch(`${ollamaUrl}/api/tags`, {
            cache: 'no-store',
            signal: AbortSignal.timeout(5000),
        })
        if (!res.ok) return NextResponse.json({ models: [] })
        const data = await res.json()
        return NextResponse.json(data)
    } catch {
        return NextResponse.json({ models: [] })
    }
}
