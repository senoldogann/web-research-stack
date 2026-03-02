declare module 'react-syntax-highlighter' {
    import { ComponentType } from 'react'

    interface SyntaxHighlighterProps {
        language?: string
        style?: any
        PreTag?: string | ComponentType<any>
        children: string
        [key: string]: any
    }

    export const Prism: ComponentType<SyntaxHighlighterProps>
    export const Light: ComponentType<SyntaxHighlighterProps>
}

declare module 'react-syntax-highlighter/dist/cjs/index.js' {
    export * from 'react-syntax-highlighter'
}

declare module 'react-syntax-highlighter/dist/esm/styles/prism' {
    export const vscDarkPlus: any
    export const atomDark: any
    export const oneDark: any
    export const materialDark: any
    export const materialLight: any
    export const prism: any
    export const tomorrow: any
}

declare module 'react-syntax-highlighter/dist/cjs/styles/prism' {
    export * from 'react-syntax-highlighter/dist/esm/styles/prism'
}

declare module 'react-syntax-highlighter/dist/esm/styles/hljs' {
    export const atomOneDark: any
    export const atomOneLight: any
    export const github: any
    export const monokai: any
    export const vs2015: any
}
