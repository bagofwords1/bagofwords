import { deleteNotes, type DeleteNotes } from '~/utils/deleteNotes'

// What deleting these conversations does to their dashboards (phase 7):
// never-published ones go with them; published ones stay, losing changes
// not yet published. The delete confirmations say so in small lines. Empty
// when the preview cannot be read: the confirmation then reads as it always
// did.
export async function dashboardsDeleteNotes(reportIds: string[]): Promise<DeleteNotes> {
    const previews = await Promise.all(reportIds.map(async (id) => {
        try {
            const { data, error } = await useMyFetch(`/reports/${id}/delete_preview`)
            if (error.value) return []
            return ((data.value as any)?.dashboards || []) as Array<{ id?: string; title?: string; outcome?: string }>
        } catch {
            return []
        }
    }))
    return deleteNotes(previews.flat())
}
