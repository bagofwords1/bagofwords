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

/** The values a filter run starts from, before the viewer changes any: for
 * each declared input, the first stored value a query declaring it was served
 * with (the step's applied_params, queries in order), else the first declared
 * default, else nothing. A stored value anywhere beats any default. Identity
 * inputs are never seeded: the server resolves them for the viewer. Returns a
 * new object; the inputs are not mutated. */
export function seedParamValues(
  specsByQuery: Record<string, any[]>,
  appliedByQuery: Record<string, Record<string, any> | null | undefined>,
): Record<string, any> {
  const seeded: Record<string, any> = {}
  const has = (name: string) => Object.prototype.hasOwnProperty.call(seeded, name)
  const seedable = (spec: any) => spec && spec.source !== 'identity' && spec.source !== 'input_identity_default'
  for (const [qid, specs] of Object.entries(specsByQuery || {})) {
    for (const spec of specs || []) {
      if (!seedable(spec) || has(spec.name)) continue
      const stored = appliedByQuery?.[qid]?.[spec.name]
      if (stored !== null && stored !== undefined) seeded[spec.name] = stored
    }
  }
  for (const specs of Object.values(specsByQuery || {})) {
    for (const spec of specs || []) {
      if (!seedable(spec) || has(spec.name)) continue
      if (spec.default !== null && spec.default !== undefined) seeded[spec.name] = spec.default
    }
  }
  return seeded
}
