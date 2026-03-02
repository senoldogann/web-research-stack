import { NextRequest, NextResponse } from 'next/server'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

const defaultOllamaUrl = () =>
    process.env.OLLAMA_URL || process.env.NEXT_PUBLIC_OLLAMA_URL || 'http://localhost:11434'

async function fetchOllamaTags(baseUrl: string, apiKey?: string): Promise<NextResponse> {
    const headers: Record<string, string> = {}
    if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`
    try {
        const res = await fetch(`${baseUrl.replace(/\/$/, '')}/api/tags`, {
            cache: 'no-store',
            headers,
            signal: AbortSignal.timeout(5000),
        })
        if (!res.ok) return NextResponse.json({ models: [] })
        const data = await res.json()
        return NextResponse.json(data)
    } catch {
        return NextResponse.json({ models: [] })
    }
}

/**
 * GET — used by legacy callers; always uses the server-side OLLAMA_URL env var.
 */
export async function GET() {
    return fetchOllamaTags(defaultOllamaUrl())
}

/**
 * POST — accepts optional { ollamaUrl, ollamaApiKey } from the client
 * to support cloud/remote Ollama endpoints with authentication.
 */
export async function POST(request: NextRequest) {
    try {
        const body = await request.json().catch(() => ({}))
        const ollamaUrl: string = body.ollamaUrl || defaultOllamaUrl()
        const ollamaApiKey: string | undefined = body.ollamaApiKey || undefined
        return fetchOllamaTags(ollamaUrl, ollamaApiKey)
    } catch {
        return NextResponse.json({ models: [] })
    }
}
