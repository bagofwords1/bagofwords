import { useI18n } from 'vue-i18n'

/**
 * Mode-aware wording for artifacts (page = dashboard, doc = document,
 * slides = presentation). A catalog key keeps its dashboard text; sibling
 * keys `${key}Doc` / `${key}Slides` hold the document / presentation text.
 * `tm(key, mode, params)` picks the variant when it exists, else the base key,
 * so every locale stays grammatical without interpolating a noun.
 */
export function artifactWordingSuffix(mode?: string | null): '' | 'Doc' | 'Slides' {
    if (mode === 'doc') return 'Doc'
    if (mode === 'slides') return 'Slides'
    return ''
}

export function useArtifactWording() {
    const { t, te } = useI18n()
    function tm(key: string, mode?: string | null, params?: Record<string, unknown>): string {
        const suffix = artifactWordingSuffix(mode)
        const variant = suffix ? `${key}${suffix}` : key
        const use = suffix && (te(variant) || te(variant, 'en')) ? variant : key
        return params ? t(use, params) : t(use)
    }
    return { tm }
}
