import type { Metadata } from 'next'
import { Inter, Newsreader } from 'next/font/google'
import './globals.css'
import { ThemeProvider } from './contexts/ThemeProvider'
import { LanguageProvider } from './contexts/LanguageProvider'

const inter = Inter({ subsets: ['latin'], variable: '--font-sans' })
const newsreader = Newsreader({ subsets: ['latin'], variable: '--font-serif', style: ['normal', 'italic'] })

export const metadata: Metadata = {
    title: 'Moonlit Chat',
    description: 'Yapay zeka analizli profesyonel araştırma platformu',
}

export default function RootLayout({
    children,
}: {
    children: React.ReactNode
}) {
    return (
        <html lang="en" suppressHydrationWarning>
            <head>
                {/* Inline script to set data-theme before first paint to prevent flash */}
                <script
                    dangerouslySetInnerHTML={{
                        __html: `(function(){try{var t=localStorage.getItem('theme');var r=t==='light'?'light':t==='system'?window.matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light':'dark';document.documentElement.setAttribute('data-theme',r)}catch(e){document.documentElement.setAttribute('data-theme','dark')}})()`,
                    }}
                />
            </head>
            <body className={`${inter.variable} ${newsreader.variable} font-sans bg-[var(--bg-primary)] text-[var(--text-primary)] antialiased`} style={{ backgroundColor: 'var(--bg-primary)', color: 'var(--text-primary)' }}>
                <ThemeProvider>
                    <LanguageProvider>
                        {children}
                    </LanguageProvider>
                </ThemeProvider>
            </body>
        </html>
    )
}
