import type { Ref } from 'vue'

export type ExportFormat = 'pdf' | 'pptx' | 'html'

export interface ExportOption {
    format: ExportFormat
    /** Row label in the menu. */
    label: string
    icon: string
}

/**
 * Which export formats an artifact can actually produce.
 *
 * Availability is a property of the artifact, not of the screen showing it:
 * a deck is python-pptx source, and a page-mode dashboard is the only thing
 * with a React runtime a standalone HTML file could stand up. A doc exports as
 * PDF like the rest — the renderer prints the app's own paper page for it.
 *
 * Both the in-app viewer and the public share page read this one list, so a
 * gate added here (a withheld snapshot, a new mode) can never be applied to
 * one toolbar and forgotten in the other — which is exactly how the share
 * page ended up with a `snapshotWithheld` check the in-app buttons never had.
 */
export function useArtifactExports(
    artifact: Ref<any>,
    opts: {
        /**
         * Viewer-identity mode on user-scoped connections: the snapshot is
         * creator data a viewer must not receive, and every export bakes the
         * snapshot in. The server refuses anyway; this hides the button.
         */
        snapshotWithheld?: Ref<boolean>
        /**
         * Extra host-side precondition, e.g. the share page only offers
         * exports once the report has loaded and actually has artifacts.
         */
        enabled?: Ref<boolean>
    } = {},
) {
    const { t } = useI18n()

    const availableExports = computed<ExportOption[]>(() => {
        const value = artifact.value
        if (!value) return []
        if (opts.enabled && !opts.enabled.value) return []
        if (opts.snapshotWithheld?.value) return []

        const mode = value.mode || 'page'
        const options: ExportOption[] = []

        // Dashboards and documents. A deck's download is the deck itself
        // (PPTX), so PDF is not offered for slides even though the renderer
        // can produce one.
        if (mode === 'page' || mode === 'doc') {
            options.push({
                format: 'pdf',
                label: t('exports.pdf'),
                icon: 'i-heroicons-document-text',
            })
        }
        if (mode === 'slides') {
            options.push({
                format: 'pptx',
                label: t('exports.pptx'),
                icon: 'i-heroicons-presentation-chart-bar',
            })
        }
        if (mode === 'page') {
            options.push({
                format: 'html',
                label: t('exports.html'),
                icon: 'i-heroicons-code-bracket',
            })
        }
        return options
    })

    return { availableExports }
}
