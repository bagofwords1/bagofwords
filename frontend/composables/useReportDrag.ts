// Dragging a report row onto a project (folder) row.
//
// Shared state rather than dataTransfer alone: the HTML5 drag API only lets
// you *read* dataTransfer inside `drop`, so `dragover`/`dragenter` (where the
// drop targets decide whether to highlight) have nothing to inspect. The ref
// below is set on dragstart and cleared on dragend, so every surface in the
// app can ask "is a report being dragged right now?".
//
// The MIME payload is still set so the drag carries a real payload (and so a
// future cross-window drop could work).

export const REPORT_DRAG_MIME = 'application/x-bow-report-id'

export interface DraggedReport {
    id: string
    title?: string | null
    projectId?: string | null
}

// Module scope = one shared instance across the layout and every page.
const draggingReport = ref<DraggedReport | null>(null)

export function useReportDrag() {
    const startReportDrag = (e: DragEvent, report: any) => {
        if (!report?.id) return
        draggingReport.value = {
            id: String(report.id),
            title: report.title ?? null,
            projectId: report.project_id ?? report.project?.id ?? null,
        }
        const dt = e.dataTransfer
        if (!dt) return
        dt.effectAllowed = 'move'
        try {
            dt.setData(REPORT_DRAG_MIME, String(report.id))
        } catch {
            // Some browsers reject custom types in odd contexts — the shared
            // ref above still carries the payload for in-app drops.
        }
    }

    const endReportDrag = () => {
        draggingReport.value = null
    }

    // Prefer the shared ref (always available); fall back to the dataTransfer
    // payload, which is only readable inside a `drop` handler.
    const droppedReportId = (e?: DragEvent): string | null => {
        if (draggingReport.value?.id) return draggingReport.value.id
        try {
            return e?.dataTransfer?.getData(REPORT_DRAG_MIME) || null
        } catch {
            return null
        }
    }

    return { draggingReport, startReportDrag, endReportDrag, droppedReportId }
}
