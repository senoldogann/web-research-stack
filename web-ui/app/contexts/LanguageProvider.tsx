'use client'

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { Translations } from '../i18n/en'
import en from '../i18n/en'
import tr from '../i18n/tr'

type Language = 'en' | 'tr'

const translationMap: Record<Language, Translations> = { en, tr }

interface LanguageContextValue {
    language: Language
    setLanguage: (lang: Language) => void
    t: Translations
}

const LanguageContext = createContext<LanguageContextValue | undefined>(undefined)

export function LanguageProvider({ children }: { children: React.ReactNode }) {
    const [language, setLanguageState] = useState<Language>('en')

    // Hydration-safe init from localStorage
    useEffect(() => {
        const stored = localStorage.getItem('language') as Language | null
        if (stored && (stored === 'en' || stored === 'tr')) {
            setLanguageState(stored)
            document.documentElement.lang = stored
        }
    }, [])

    const setLanguage = useCallback((lang: Language) => {
        setLanguageState(lang)
        localStorage.setItem('language', lang)
        document.documentElement.lang = lang
    }, [])

    const t = translationMap[language]

    const value = useMemo(
        () => ({ language, setLanguage, t }),
        [language, setLanguage, t],
    )

    return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>
}

export function useLanguage(): LanguageContextValue {
    const ctx = useContext(LanguageContext)
    if (!ctx) throw new Error('useLanguage must be used within a LanguageProvider')
    return ctx
}
