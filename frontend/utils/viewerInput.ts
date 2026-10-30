// A published chart that waits for the viewer's input: its query needs a value
// only the app gives (useParams().setParams), so the reader's dashboard run did
// not run it and left a "waiting" result (viewer_result.needs_input). Pure, so
// it is tested on its own (tests/unit/viewerInput.mjs).

/** Whether the reader's result is a wait for their input (nothing ran). */
export function awaitsViewerInput(viewerResult?: { needs_input?: unknown } | null): boolean {
  const names = viewerResult?.needs_input
  return Array.isArray(names) && names.length > 0
}

/** Whether the reader's result is a successful run of their own. A wait for
 * input is not one: it holds no rows (the export rule, dashboardExportsHidden). */
export function isOwnSuccess(viewerResult?: { status?: unknown, needs_input?: unknown } | null): boolean {
  return viewerResult?.status === 'success' && !awaitsViewerInput(viewerResult)
}
