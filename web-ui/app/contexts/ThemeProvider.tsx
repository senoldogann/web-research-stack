'use client'

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

type Theme = 'light' | 'dark' | 'system'
type ResolvedTheme = 'light' | 'dark'

interface ThemeContextValue {
    theme: Theme
    setTheme: (theme: Theme) => void
    resolvedTheme: ResolvedTheme
}

const ThemeContext = createContext<ThemeContextValue | undefined>(undefined)

function getSystemTheme(): ResolvedTheme {
    if (typeof window === 'undefined') return 'dark'
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function applyTheme(resolved: ResolvedTheme) {
    document.documentElement.setAttribute('data-theme', resolved)
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
    const [theme, setThemeState] = useState<Theme>('dark')
    const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>('dark')
    const [mounted, setMounted] = useState(false)

    // Hydration-safe init from localStorage
    useEffect(() => {
        const stored = localStorage.getItem('theme') as Theme | null
        const initial = stored && ['light', 'dark', 'system'].includes(stored) ? stored : 'dark'
        setThemeState(initial)

        const resolved = initial === 'system' ? getSystemTheme() : initial
        setResolvedTheme(resolved)
        applyTheme(resolved)
        setMounted(true)
    }, [])

    // Listen for system theme changes when in 'system' mode
    useEffect(() => {
        if (!mounted) return

        const mql = window.matchMedia('(prefers-color-scheme: dark)')
        const handler = () => {
            if (theme === 'system') {
                const resolved = getSystemTheme()
                setResolvedTheme(resolved)
                applyTheme(resolved)
            }
        }
        mql.addEventListener('change', handler)
        return () => mql.removeEventListener('change', handler)
    }, [theme, mounted])

    const setTheme = useCallback((next: Theme) => {
        setThemeState(next)
        localStorage.setItem('theme', next)
        const resolved = next === 'system' ? getSystemTheme() : next
        setResolvedTheme(resolved)
        applyTheme(resolved)
    }, [])

    const value = useMemo(
        () => ({ theme, setTheme, resolvedTheme }),
        [theme, setTheme, resolvedTheme],
    )

    return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
    const ctx = useContext(ThemeContext)
    if (!ctx) throw new Error('useTheme must be used within a ThemeProvider')
    return ctx
}
