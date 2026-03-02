import { NextRequest, NextResponse } from 'next/server'

export const runtime = 'nodejs'
export const maxDuration = 300

export async function POST(request: NextRequest) {
    try {
        const body = await request.json()
        const { query, maxSources, deepMode, research_profile, ollamaApiKey, ollamaBaseUrl } = body

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
        }

        if (backendApiKey) {
            headers['X-API-Key'] = backendApiKey
        }

        const response = await fetch(`${backendUrl}/api/v1/tools/web-research`, {
            method: 'POST',
            headers,
            body: JSON.stringify({
                query,
                max_sources: maxSources,
                deep_mode: deepMode,
                research_profile: research_profile || 'technical',
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

        const result = await response.json()
        return NextResponse.json(result)
    } catch (error) {
        console.error('Research API error:', error)
        return NextResponse.json(
            {
                error: 'Research failed',
                details: String(error),
                message: 'Make sure the FastAPI backend is running on port 8000 (python -m web_scraper.api)'
            },
            { status: 500 }
        )
    }
}
