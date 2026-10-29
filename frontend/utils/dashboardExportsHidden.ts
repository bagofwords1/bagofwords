export interface ExportsState {
  source: 'report' | 'dashboard' | 'draft'
  mode: string
  // Some chart's shared snapshot is withheld from this reader.
  snapshotWithheld: boolean
  // /d meta: this reader's exports cannot hold the shared snapshot.
  exportsOwnRows: boolean
  // The reader holds a successful run of their own of some chart.
  hasOwnSuccess: boolean
}

// Whether the page hides its export button: exactly when the server would
// refuse this reader. /d draws a page or doc export for the request from the
// rows this reader sees (a chart they may not read prints empty, as on the
// page), so it refuses only a personal dashboard's reader with no successful
// run of their own, and a personal deck (drawn once, from the owner's rows).
// /r still refuses any withheld snapshot.
export function exportsHidden(s: ExportsState): boolean {
  if (s.source === 'report') return s.snapshotWithheld
  if (!s.exportsOwnRows) return false
  return s.mode === 'slides' || !s.hasOwnSuccess
}
