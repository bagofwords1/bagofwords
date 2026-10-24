// Where the shared dashboard page (SharedDashboardView) reads and runs.
//
// One page, three sources with the same calls:
// - 'report': a conversation's /r/{id} page, as it always was;
// - 'dashboard': a published dashboard's /d/{id} page;
// - 'draft': a dashboard's draft, for its editors, before it is published.
// A call a source does not have is null, and the page hides what needs it.

export type SharedDashboardSource = 'report' | 'dashboard' | 'draft'

export interface SharedDashboardApi {
    pagePath: string
    meta: string
    artifacts: string | null
    artifact: ((artifactId: string) => string) | null
    content: string | null
    queries: (artifactId?: string) => string
    step: (queryId: string) => string
    embedToken: (fileId: string) => string
    run: ((artifactId?: string) => string) | null
    refreshOnView: string | null
    paramRun: (queryId: string) => string
    exportUrl: ((format: 'pdf' | 'pptx' | 'html', artifactId?: string) => string) | null
    fork: string | null
    chatBase: string | null
    previewsBase: string | null
}

export function sharedDashboardApi(source: SharedDashboardSource, id: string): SharedDashboardApi {
    const artifactParam = (artifactId?: string) => (artifactId ? `?artifact_id=${encodeURIComponent(artifactId)}` : '')
    if (source === 'report') {
        const r = `/api/r/${id}`
        return {
            pagePath: `/r/${id}`,
            meta: r,
            artifacts: `${r}/artifacts`,
            artifact: (artifactId) => `${r}/artifacts/${artifactId}`,
            content: null,
            queries: (artifactId) => `${r}/queries${artifactId ? `?artifact_id=${artifactId}` : ''}`,
            step: (queryId) => `${r}/queries/${queryId}/step`,
            embedToken: (fileId) => `${r}/files/${fileId}/embed_token`,
            run: (artifactId) => `${r}/run${artifactId ? `?artifact_id=${artifactId}` : ''}`,
            refreshOnView: `${r}/rerun`,
            paramRun: (queryId) => `/api/queries/${queryId}/run`,
            exportUrl: (format, artifactId) => (format === 'pdf'
                ? `${r}/export_pdf`
                : `${r}/export_${format}${artifactParam(artifactId)}`),
            fork: `/api/reports/${id}/fork`,
            chatBase: `/r/${id}`,
            previewsBase: null,
        }
    }
    const d = `/api/d/${encodeURIComponent(id)}`
    if (source === 'dashboard') {
        return {
            pagePath: `/d/${id}`,
            meta: d,
            artifacts: null,
            artifact: null,
            content: `${d}/content`,
            queries: () => `${d}/queries`,
            step: (queryId) => `${d}/queries/${queryId}/step`,
            embedToken: (fileId) => `${d}/files/${fileId}/embed_token`,
            run: () => `${d}/run`,
            refreshOnView: `${d}/refresh`,
            paramRun: (queryId) => `${d}/queries/${queryId}/run`,
            exportUrl: (format) => `${d}/export/${format}`,
            fork: `${d}/fork`,
            chatBase: `/d/${encodeURIComponent(id)}`,
            previewsBase: `/d/${encodeURIComponent(id)}/previews`,
        }
    }
    return {
        pagePath: `/d/${id}/draft`,
        meta: `${d}/draft`,
        artifacts: null,
        artifact: null,
        content: `${d}/draft/content`,
        queries: () => `${d}/draft/queries`,
        step: (queryId) => `${d}/draft/queries/${queryId}/step`,
        embedToken: (fileId) => `${d}/draft/files/${fileId}/embed_token`,
        run: null,
        refreshOnView: null,
        // A filter on the draft (collab01): the version shown may be another
        // editor's conversation, so the run goes through the draft door and
        // runs as the reader, kept as theirs alone.
        paramRun: (queryId) => `${d}/draft/queries/${queryId}/run`,
        exportUrl: null,
        fork: null,
        chatBase: null,
        previewsBase: null,
    }
}
