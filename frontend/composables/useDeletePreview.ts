// The never-published dashboards that deleting these conversations deletes
// with them (phase 7: published ones stay, the others go). The delete
// confirmations name them in one small line. Empty when the preview cannot
// be read: the confirmation then reads as it always did.
export async function unpublishedDashboardsDeletedWith(reportIds: string[]): Promise<string[]> {
    const previews = await Promise.all(reportIds.map(async (id) => {
        try {
            const { data, error } = await useMyFetch(`/reports/${id}/delete_preview`)
            if (error.value) return []
            return ((data.value as any)?.dashboards || []) as Array<{ title?: string; outcome?: string }>
        } catch {
            return []
        }
    }))
    return previews.flat().filter((d) => d.outcome === 'deleted').map((d) => d.title || 'Untitled')
}
