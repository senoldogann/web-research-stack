import { NextRequest, NextResponse } from 'next/server'

export const maxDuration = 300 // Deep research can legitimately run longer
export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

export async function POST(request: NextRequest) {
    try {
        const body = await request.json()
        const { query, maxSources, deepMode, research_profile, model, provider, openaiApiKey, ollamaApiKey, ollamaBaseUrl } = body

        if (!query || typeof query !== 'string') {
            return NextResponse.json(
                { error: 'Query is required' },
                { status: 400 }
            )
        }

        const backendUrl =
            process.env.BACKEND_BASE_URL ||
            process.env.NEXT_PUBLIC_BACKEND_URL ||
            'http://localhost:8000'
        const backendApiKey = process.env.BACKEND_API_KEY || process.env.WEB_SCRAPER_API_KEY
        const headers: Record<string, string> = {
            'Content-Type': 'application/json',
            Accept: 'text/event-stream',
        }

        if (backendApiKey) {
            headers['X-API-Key'] = backendApiKey
        }

        const response = await fetch(`${backendUrl}/api/v1/tools/web-research/stream`, {
            method: 'POST',
            headers,
            body: JSON.stringify({
                query,
                max_sources: maxSources,
                deep_mode: deepMode,
                research_profile: research_profile || 'auto',
                model,
                provider: provider || 'ollama',
                openai_api_key: openaiApiKey || null,
                ollama_api_key: ollamaApiKey || null,
                ollama_base_url: ollamaBaseUrl || null,
            }),
        })

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}))
            return NextResponse.json(
                { error: 'Research failed', details: errorData },
                { status: response.status }
            )
        }

        return new Response(response.body, {
            status: response.status,
            headers: {
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache, no-transform',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
            },
        })
    } catch (error) {
        console.error('Research API stream error:', error)
        return NextResponse.json(
            {
                error: 'Stream failed',
                details: String(error),
            },
            { status: 500 }
        )
    }
}
