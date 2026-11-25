import { toRaw } from 'vue'

/**
 * What a dashboard's frame receives (ARTIFACT_DATA, and its srcdoc seed),
 * built without copying the rows on the page. Used by the in-app dashboard
 * (ArtifactFrame.vue) and the shared one (SharedDashboardView.vue).
 *
 * The rows go as they are (toRaw: plain arrays, never Vue proxies), so the
 * browser's own copy (postMessage, or the srcdoc's JSON text) is the only one.
 * They came as JSON from the API and the pages never edit them. Everything
 * else is small and still goes through JSON, so the frame sees the values it
 * always saw: a field set to undefined stays missing, NaN stays null, a Date
 * stays text.
 *
 * `separateRows` (postMessage): charts of one query hold one rows array on the
 * page; as before, each chart gets its own in the frame, so dashboard code
 * that sorts one chart's rows in place does not reorder another's. (The srcdoc
 * seed needs none: its JSON text separates them.)
 */
export function artifactFramePayload(fields: Record<string, any>, { separateRows = false } = {}): any {
  const payload = JSON.parse(JSON.stringify({ ...fields, visualizations: null }))
  const seen = new Set<any>()
  payload.visualizations = (toRaw(fields.visualizations) as any[]).map((viz: any) => {
    const raw = toRaw(viz)
    if (raw?.rows === undefined) return JSON.parse(JSON.stringify(raw ?? null))
    const copy = JSON.parse(JSON.stringify({ ...raw, rows: null }))
    let rows = toRaw(raw.rows)
    if (separateRows && rows && typeof rows === 'object') {
      if (seen.has(rows)) rows = separateCopy(rows)
      else seen.add(rows)
    }
    copy.rows = rows
    return copy
  })
  return payload
}

function separateCopy(rows: any) {
  try { return structuredClone(rows) } catch { return JSON.parse(JSON.stringify(rows)) }
}

let cloneFallbackWarned = false

/**
 * Post a frame's data. A message the browser refuses to copy (DataCloneError)
 * goes as a JSON copy, as every ARTIFACT_DATA did before: slower, never lost,
 * with one console warning. Any other failure is the caller's.
 */
export function postWithJsonFallback(target: { postMessage(message: any, targetOrigin: string): void }, message: any): void {
  try {
    target.postMessage(message, '*')
  } catch (e: any) {
    if (e?.name !== 'DataCloneError') throw e
    if (!cloneFallbackWarned) {
      cloneFallbackWarned = true
      console.warn('[dashboard] The frame data could not be posted as is; sent a JSON copy instead.', e)
    }
    target.postMessage(JSON.parse(JSON.stringify(message)), '*')
  }
}
